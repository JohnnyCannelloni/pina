"""Filling the place cache from a Geofabrik OpenStreetMap extract.

READ THIS THIRD, after migration 0005 and app/regions.py. It replaced the
Overpass client, and the contrast is the lesson.

**What was wrong before.** The old path asked a shared public API
(`overpass-api.de`) for places tile by tile, on demand. Measured across an
afternoon it returned 502 "all query workers are busy", a 25-second read
timeout, or 429, on the large majority of attempts — and one of the 429s was
our own fault for retrying too eagerly. No amount of retry logic fixes a
dependency that is busy by default.

**What this does instead.** Geofabrik publishes OpenStreetMap as static files,
one per region. A static file has no query workers to be busy and no rate limit
to exceed; it is just bytes over HTTPS, resumable, cacheable on disk. We
download a country once and parse it locally.

The numbers, measured on Malta (8.6 MB): **1,749 named restaurants, cafés and
sights extracted in under a second.** The Overpass path spent minutes failing
to fetch a dozen.

**Two things to notice in the code:**

1. `_PoiHandler` keeps *everything* in the region, not just one city's box —
   see its docstring for why that is both correct and cheap. One import makes
   every viewport in the region instantly available.
2. The download is resumable and cached. Re-importing a region you already have
   costs no network at all, which is what makes adding a second city in the
   same country take seconds.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import httpx
import osmium
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Category, ExtractState, OsmExtract, OsmPlace
from app.regions import find_region

logger = logging.getLogger(__name__)

# --- OSM tags -> Pina categories -------------------------------------------
#
# The same mapping the Overpass client used, kept because the categories are
# ours and have to satisfy ck_osm_places_category. Two dicts rather than one
# because OSM splits these across two different keys.
AMENITY_CATEGORIES: dict[str, Category] = {
    "restaurant": Category.RESTAURANT,
    "fast_food": Category.RESTAURANT,
    "cafe": Category.CAFE,
    "ice_cream": Category.CAFE,
}

TOURISM_CATEGORIES: dict[str, Category] = {
    "attraction": Category.SIGHT,
    "museum": Category.SIGHT,
    "gallery": Category.SIGHT,
    "viewpoint": Category.SIGHT,
}

# --- OSM tags worth keeping --------------------------------------------------
#
# Copied verbatim into osm_places.tags (migration 0006). Yes/no-style tags keep
# OSM's own value ("yes", "no", "only", "limited") rather than being squashed to
# a boolean: "only" (a vegan-only café) is worth more than "yes", and the search
# in app/ask.py decides which values count as a match.
FEATURE_TAGS: tuple[str, ...] = (
    "outdoor_seating",
    "diet:vegetarian",
    "diet:vegan",
    "diet:gluten_free",
    "wheelchair",
    "takeaway",
    "delivery",
)

#: Free text kept as written. Shown to the user; never used to filter.
TEXT_TAGS: tuple[str, ...] = ("opening_hours", "addr:street")


def _clean_website(value: str | None) -> str | None:
    """A website the UI can safely put in a link, or None.

    This text ends up in an <a href>, so only http(s) is accepted — a
    `javascript:` URL in OSM data would otherwise run in the user's browser.
    A bare "www.example.pt" is common in OSM and gets https:// added.
    """
    if not value:
        return None
    url = value.split(";")[0].strip()
    lowered = url.lower()
    if lowered.startswith(("http://", "https://")):
        return url
    if lowered.startswith("www."):
        return f"https://{url}"
    return None


def place_tags(tags: object) -> dict[str, object] | None:
    """The subset of a node's OSM tags that osm_places keeps.

    `tags` is anything with a `.get(key)`: an osmium TagList in the importer, a
    plain dict in tests. Returns None rather than {} when nothing is worth
    keeping, so "no details" has one representation in the database.
    """
    kept: dict[str, object] = {}

    # Cuisine is a ;-separated list in OSM ("italian;pizza"). Stored as a JSON
    # list so `tags @> '{"cuisine": ["italian"]}'` matches either value.
    cuisine = tags.get("cuisine")  # type: ignore[attr-defined]
    if cuisine:
        values = [
            part.strip().lower().replace(" ", "_")
            for part in cuisine.split(";")
            if part.strip()
        ]
        if values:
            kept["cuisine"] = values

    for key in FEATURE_TAGS + TEXT_TAGS:
        value = tags.get(key)  # type: ignore[attr-defined]
        if value:
            kept[key] = value.strip()

    website = _clean_website(
        tags.get("website") or tags.get("contact:website")  # type: ignore[attr-defined]
    )
    if website:
        kept["website"] = website

    phone = tags.get("phone") or tags.get("contact:phone")  # type: ignore[attr-defined]
    if phone:
        # Several numbers are ;-separated. The first is the one to dial.
        first = phone.split(";")[0].strip()
        if first:
            kept["phone"] = first

    return kept or None


#: Rows per INSERT. Large enough that 1,700 places is one or two statements,
#: small enough that a country with 200,000 cafés does not build a single
#: enormous statement and exhaust memory.
BATCH_SIZE = 1000

#: Progress log cadence while streaming the download.
LOG_EVERY_BYTES = 32 * 1024 * 1024


class ExtractError(RuntimeError):
    """The extract could not be downloaded or parsed."""


class ExtractCancelled(Exception):
    """The download was stopped on purpose, because nothing needs it any more.

    Deliberately *not* an ExtractError. A failure is recorded and offered for
    retry; a cancellation is the user's intent, and must not show up as
    "Some places could not be loaded".
    """


def cache_dir() -> Path:
    """Where downloaded extracts live.

    On disk rather than in the database: these are large, opaque, and
    re-downloadable. Keeping them as files means `rm -rf` is a valid cache
    reset and Postgres never has to hold a 600 MB blob.
    """
    path = Path(settings.EXTRACT_CACHE_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_path(region: str) -> Path:
    """Local file for a region, e.g. europe/malta -> europe_malta.osm.pbf."""
    return cache_dir() / f"{region.replace('/', '_')}-latest.osm.pbf"


def download_extract(
    region: str,
    force: bool = False,
    cancel: threading.Event | None = None,
) -> Path:
    """Fetch a region's extract, resuming or reusing where possible.

    Returns the local path. Raises ExtractError on failure.

    `cancel` is checked once per chunk (1 MB), so setting it stops a download
    within a second or so. A cancelled download deletes its `.part` file — the
    point of cancelling is not to leave several hundred megabytes behind — and
    raises ExtractCancelled.
    """
    target = extract_path(region)
    partial = target.with_suffix(target.suffix + ".part")

    if target.exists() and not force:
        logger.info(
            "extract %s already downloaded (%.1f MB)",
            region,
            target.stat().st_size / 1_048_576,
        )
        return target

    # The URL comes from Geofabrik's own index, not from string-building. The
    # region id and its URL path differ — "us/new-york" is published under
    # north-america/ — so constructing the URL would guess wrong exactly where
    # it matters most.
    url = find_region(region).pbf_url
    # Resume from however much of a previous attempt survived. A 600 MB
    # download over a flaky connection will be interrupted; starting from zero
    # each time is how it never finishes.
    resume_from = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": settings.USER_AGENT}
    if resume_from:
        headers["Range"] = f"bytes={resume_from}-"
        logger.info("resuming %s from %.1f MB", region, resume_from / 1_048_576)

    try:
        with httpx.stream(
            "GET",
            url,
            headers=headers,
            timeout=httpx.Timeout(30.0, read=settings.EXTRACT_READ_TIMEOUT_SECONDS),
            follow_redirects=True,
        ) as response:
            if response.status_code == 404:
                raise ExtractError(
                    f"Geofabrik has no extract at '{region}'. "
                    "The region name in app/regions.py may be wrong."
                )
            # 206 means our Range header was honoured; 200 means the server
            # ignored it and is sending the whole file, so anything already
            # downloaded must be discarded rather than appended to.
            if resume_from and response.status_code == 200:
                logger.info("server ignored Range; restarting %s", region)
                resume_from = 0
            if response.is_error:
                raise ExtractError(
                    f"Could not download {region} (HTTP {response.status_code})."
                )

            mode = "ab" if resume_from else "wb"
            written = resume_from
            next_log = written + LOG_EVERY_BYTES

            with partial.open(mode) as handle:
                for chunk in response.iter_bytes(chunk_size=1 << 20):
                    if cancel is not None and cancel.is_set():
                        raise ExtractCancelled(f"Download of {region} cancelled.")
                    handle.write(chunk)
                    written += len(chunk)
                    if written >= next_log:
                        logger.info(
                            "%s: %.0f MB downloaded", region, written / 1_048_576
                        )
                        next_log = written + LOG_EVERY_BYTES
    except ExtractCancelled:
        # Both `with` blocks have closed the file and the connection by now.
        partial.unlink(missing_ok=True)
        logger.info("download of %s cancelled; partial file removed", region)
        raise
    except httpx.TimeoutException as exc:
        raise ExtractError(
            "The download timed out. It resumes where it left off, so "
            "running the import again will continue rather than restart."
        ) from exc
    except httpx.HTTPError as exc:
        raise ExtractError(f"Could not reach Geofabrik: {exc}") from exc

    # Rename only once the download is complete, so a half-written file is
    # never mistaken for a usable extract. The .part suffix is the whole
    # mechanism: presence of the final name means "this file is good".
    partial.replace(target)
    logger.info(
        "downloaded %s (%.1f MB)", region, target.stat().st_size / 1_048_576
    )
    return target


class _PoiHandler:
    """Collects every named, categorised place in the extract.

    Note what this class *lost* in migration 0005: it used to take a list of
    destinations and test each node against every destination's bounding box,
    emitting one row per matching destination. All of that is gone.

    Why it could go: `osm_places` is now global, so there is no destination to
    file a place under, and a whole region's places are small enough to keep
    wholesale — New York State is roughly 31,000 restaurants, cafés and sights,
    about 4 MB of rows. Importing the region once means every viewport inside it
    is served instantly, with no per-city filtering and no lazy loading.

    Not an osmium.SimpleHandler subclass: pyosmium 4's FileProcessor is a plain
    iterator, which keeps this ordinary testable Python rather than a callback
    class with framework-managed lifetime.
    """

    def __init__(self, region: str) -> None:
        self._region = region
        self.rows: list[dict[str, object]] = []

    def consider(self, node: osmium.osm.Node) -> None:
        tags = node.tags

        name = tags.get("name")
        # Unnamed places are dropped: OSM is full of them and "Restaurant" with
        # no name is not something you can usefully save to a trip.
        if not name:
            return

        category = AMENITY_CATEGORIES.get(
            tags.get("amenity", "")
        ) or TOURISM_CATEGORIES.get(tags.get("tourism", ""))
        if category is None:
            return

        location = node.location
        if not location.valid():
            return

        self.rows.append(
            {
                "source_ref": f"node/{node.id}",
                "name": name,
                "category": str(category),
                "latitude": location.lat,
                "longitude": location.lon,
                "region": self._region,
                "tags": place_tags(tags),
                "fetched_at": datetime.now(UTC),
            }
        )


def _flush(db: Session, rows: list[dict[str, object]]) -> int:
    """Upsert a batch of places. Returns how many rows were sent."""
    if not rows:
        return 0

    statement = pg_insert(OsmPlace).values(rows)
    db.execute(
        statement.on_conflict_do_update(
            constraint="uq_osm_places_source_ref",
            set_={
                "name": statement.excluded.name,
                "category": statement.excluded.category,
                "latitude": statement.excluded.latitude,
                "longitude": statement.excluded.longitude,
                "region": statement.excluded.region,
                # Without this a re-import would keep stale tags forever — and
                # rows imported before 0006 would never get any.
                "tags": statement.excluded.tags,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )
    db.commit()
    return len(rows)


def import_extract(db: Session, region: str) -> int:
    """Parse a downloaded extract and cache every place it contains.

    Returns the number of place rows written. Idempotent, thanks to the upsert:
    re-importing updates rather than duplicating, so it is safe to re-run after
    an interruption or to refresh stale data.

    No destination argument, and no bounding box: the whole region goes in. See
    _PoiHandler for why that is both correct and cheap.
    """
    path = extract_path(region)
    if not path.exists():
        raise ExtractError(f"Extract for {region} has not been downloaded yet.")

    handler = _PoiHandler(region)
    written = 0

    logger.info("importing %s", region)

    # Only NODE, and only objects carrying one of the two keys we map. The
    # KeyFilter is applied inside osmium's C++ layer, so the vast majority of a
    # country's objects — roads, buildings, boundaries — never reach Python at
    # all. That filter is why 8.6 MB parses in under a second.
    processor = osmium.FileProcessor(
        str(path), osmium.osm.NODE
    ).with_filter(osmium.filter.KeyFilter("amenity", "tourism"))

    for node in processor:
        handler.consider(node)
        if len(handler.rows) >= BATCH_SIZE:
            written += _flush(db, handler.rows)
            handler.rows = []

    written += _flush(db, handler.rows)

    # Known limitation, stated rather than hidden: a restaurant mapped as a
    # building outline is a `way`, not a `node`, and is skipped. Resolving ways
    # to a point needs a node-location cache and roughly triples the work.
    # Nodes alone gave 1,749 places for Malta, which is ample; this is a sharper
    # trade to make once the simple version is in use.
    logger.info("imported %s places from %s", written, region)
    return written


def extract_state(db: Session, region: str) -> OsmExtract:
    """The tracking row for a region, created on first use."""
    existing = db.scalar(select(OsmExtract).where(OsmExtract.region == region))
    if existing is not None:
        return existing

    extract = OsmExtract(region=region, state=ExtractState.PENDING)
    db.add(extract)
    db.commit()
    db.refresh(extract)
    return extract
