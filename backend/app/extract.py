"""fill the place cache from a geofabrik openstreetmap extract.

downloads a region's .osm.pbf once (resumable, cached on disk) and imports
every named restaurant, café and sight in it into osm_places.
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

# --- osm tags -> pina categories ---------------------------------------------
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

# --- osm tags worth keeping --------------------------------------------------
#
# stored in osm_places.tags. values are kept as osm writes them ("yes", "only",
# "limited"); app/ask.py decides which count as a match.
FEATURE_TAGS: tuple[str, ...] = (
    "outdoor_seating",
    "diet:vegetarian",
    "diet:vegan",
    "diet:gluten_free",
    "wheelchair",
    "takeaway",
    "delivery",
)

#: free text, shown but never filtered on.
TEXT_TAGS: tuple[str, ...] = ("opening_hours", "addr:street")


def _clean_website(value: str | None) -> str | None:
    """the website if it's safe to link to, else None.

    only http(s) is allowed since it ends up in an href. bare "www." gets
    https:// added.
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
    """the subset of osm tags osm_places keeps, or None if there's nothing.

    `tags` is anything with .get(): an osmium TagList, or a dict in tests.
    """
    kept: dict[str, object] = {}

    # "italian;pizza" -> ["italian", "pizza"], so containment queries match either.
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
        # first of a ;-separated list.
        first = phone.split(";")[0].strip()
        if first:
            kept["phone"] = first

    return kept or None


#: rows per insert.
BATCH_SIZE = 1000

#: how often to log download progress.
LOG_EVERY_BYTES = 32 * 1024 * 1024


class ExtractError(RuntimeError):
    """the extract couldn't be downloaded or parsed."""


class ExtractCancelled(Exception):
    """the download was stopped because nothing needs it any more.

    not an ExtractError: a cancellation shouldn't be reported as a failure.
    """


def cache_dir() -> Path:
    """where downloaded extracts live. safe to delete."""
    path = Path(settings.EXTRACT_CACHE_DIR).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def extract_path(region: str) -> Path:
    """local file for a region, e.g. us/new-york -> us_new-york-latest.osm.pbf."""
    return cache_dir() / f"{region.replace('/', '_')}-latest.osm.pbf"


def download_extract(
    region: str,
    force: bool = False,
    cancel: threading.Event | None = None,
) -> Path:
    """fetch a region's extract, reusing or resuming what's on disk.

    `cancel` is checked once per 1 mb chunk; a cancelled download removes its
    .part file and raises ExtractCancelled. raises ExtractError on failure.
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

    # use the url from the index: the id and url path differ (us/new-york
    # lives under north-america/).
    url = find_region(region).pbf_url
    # resume from any partial download.
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
            # 206 = range honoured. 200 = server ignored it, so start over.
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
        # the file and connection are closed by now.
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

    # rename only when complete, so a half-written file is never used.
    partial.replace(target)
    logger.info(
        "downloaded %s (%.1f MB)", region, target.stat().st_size / 1_048_576
    )
    return target


class _PoiHandler:
    """collects every named, categorised node in the extract.

    keeps the whole region rather than one city, so any viewport inside it is
    served without another import.
    """

    def __init__(self, region: str) -> None:
        self._region = region
        self.rows: list[dict[str, object]] = []

    def consider(self, node: osmium.osm.Node) -> None:
        tags = node.tags

        name = tags.get("name")
        # unnamed places aren't useful to save.
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
    """upsert a batch of places. returns how many rows were sent."""
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
                # so a re-import refreshes tags too.
                "tags": statement.excluded.tags,
                "fetched_at": statement.excluded.fetched_at,
            },
        )
    )
    db.commit()
    return len(rows)


def import_extract(db: Session, region: str) -> int:
    """parse a downloaded extract and cache every place in it.

    idempotent thanks to the upsert. returns the number of rows written.
    """
    path = extract_path(region)
    if not path.exists():
        raise ExtractError(f"Extract for {region} has not been downloaded yet.")

    handler = _PoiHandler(region)
    written = 0

    logger.info("importing %s", region)

    # nodes only, filtered by key inside osmium so most objects never reach python.
    processor = osmium.FileProcessor(
        str(path), osmium.osm.NODE
    ).with_filter(osmium.filter.KeyFilter("amenity", "tourism"))

    for node in processor:
        handler.consider(node)
        if len(handler.rows) >= BATCH_SIZE:
            written += _flush(db, handler.rows)
            handler.rows = []

    written += _flush(db, handler.rows)

    # known limitation: places mapped as building outlines (ways) are skipped.
    logger.info("imported %s places from %s", written, region)
    return written


def extract_state(db: Session, region: str) -> OsmExtract:
    """the tracking row for a region, created on first use."""
    existing = db.scalar(select(OsmExtract).where(OsmExtract.region == region))
    if existing is not None:
        return existing

    extract = OsmExtract(region=region, state=ExtractState.PENDING)
    db.add(extract)
    db.commit()
    db.refresh(extract)
    return extract
