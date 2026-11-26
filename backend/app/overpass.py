"""load a destination's places from the overpass api, one query per destination.

the query asks for named nodes, ways and relations with the categories below,
inside the destination's box, with `out center tags` so building outlines
(many museums) come back with a centre point. results are upserted into the
shared osm_places cache.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Category, OsmPlace

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

# --- limits ------------------------------------------------------------------

#: largest box side we'll send, in degrees (~67 km north-south). new
#: destinations are clamped to this in app/sync.py, so only older rows hit it.
MAX_SPAN_DEGREES = 0.6

#: tries per query, for responses that mean "busy, try later".
MAX_ATTEMPTS = 3

#: overpass answers these when it's overloaded or rate-limiting us. the public
#: instance also returns 502/503 from its proxy under load.
RETRY_STATUSES = frozenset({429, 502, 503, 504})

#: waits between attempts, in seconds (2 s, then 4 s).
BACKOFF_SECONDS = (2.0, 4.0)

#: cap on a server-supplied Retry-After.
MAX_RETRY_AFTER_SECONDS = 30.0

#: rows per insert.
BATCH_SIZE = 1000

# patched out in tests, so retries don't actually wait.
sleep = time.sleep


class OverpassError(RuntimeError):
    """the query couldn't be run or answered usefully. shown to the user."""


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

    `tags` is anything with .get(), such as an overpass element's tag dict.
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


def check_box(south: float, west: float, north: float, east: float) -> None:
    """raise OverpassError if the box is too large to query in one go."""
    if north - south > MAX_SPAN_DEGREES or east - west > MAX_SPAN_DEGREES:
        raise OverpassError(
            f"This destination's area is too large to load in one go "
            f"(at most {MAX_SPAN_DEGREES}° per side). Try adding a smaller "
            "area, such as the city itself rather than its region."
        )


def build_query(
    south: float, west: float, north: float, east: float, timeout: int
) -> str:
    """the overpass ql for every named, categorised place in the box.

    the value filters are built from the category dicts, so the query can't
    drift from what parse_elements accepts.
    """
    box = f"({south},{west},{north},{east})"
    amenities = "|".join(sorted(AMENITY_CATEGORIES))
    tourism = "|".join(sorted(TOURISM_CATEGORIES))
    return (
        f"[out:json][timeout:{timeout}];\n"
        "(\n"
        f'  nwr["amenity"~"^({amenities})$"]["name"]{box};\n'
        f'  nwr["tourism"~"^({tourism})$"]["name"]{box};\n'
        ");\n"
        "out center tags;\n"
    )


def parse_elements(elements: list[dict]) -> list[dict[str, object]]:
    """overpass elements -> osm_places rows.

    nodes carry lat/lon; ways and relations carry a `center`. source_ref is
    "<type>/<id>", so node/123 and way/123 stay distinct, and node refs match
    the format saved places already use.
    """
    rows: list[dict[str, object]] = []
    now = datetime.now(UTC)

    for element in elements:
        kind = element.get("type")
        osm_id = element.get("id")
        tags = element.get("tags") or {}
        if kind not in ("node", "way", "relation") or osm_id is None:
            continue

        name = tags.get("name")
        # unnamed places aren't useful to save.
        if not name:
            continue

        # amenity first, as the old importer did.
        category = AMENITY_CATEGORIES.get(
            tags.get("amenity", "")
        ) or TOURISM_CATEGORIES.get(tags.get("tourism", ""))
        if category is None:
            continue

        point = element if kind == "node" else (element.get("center") or {})
        latitude, longitude = point.get("lat"), point.get("lon")
        if latitude is None or longitude is None:
            continue

        rows.append(
            {
                "source_ref": f"{kind}/{osm_id}",
                "name": name,
                "category": str(category),
                "latitude": float(latitude),
                "longitude": float(longitude),
                "tags": place_tags(tags),
                "fetched_at": now,
            }
        )

    return rows


def store_places(db: Session, rows: list[dict[str, object]]) -> int:
    """upsert rows into osm_places, in batches. returns how many were sent."""
    written = 0
    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start : start + BATCH_SIZE]
        statement = pg_insert(OsmPlace).values(batch)
        db.execute(
            statement.on_conflict_do_update(
                constraint="uq_osm_places_source_ref",
                set_={
                    "name": statement.excluded.name,
                    "category": statement.excluded.category,
                    "latitude": statement.excluded.latitude,
                    "longitude": statement.excluded.longitude,
                    # so a re-sync refreshes tags too.
                    "tags": statement.excluded.tags,
                    "fetched_at": statement.excluded.fetched_at,
                },
            )
        )
        written += len(batch)
    db.commit()
    return written


def _wait_before_retry(response: httpx.Response, attempt: int) -> float:
    """seconds to wait: the server's Retry-After if sensible, else our backoff."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(max(float(header), 0.0), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]


class OverpassClient:
    """posts one query per destination to an overpass instance."""

    def fetch(
        self, south: float, west: float, north: float, east: float
    ) -> list[dict]:
        """every matching element in the box. raises OverpassError on failure."""
        check_box(south, west, north, east)
        timeout = settings.OVERPASS_TIMEOUT_SECONDS
        query = build_query(south, west, north, east, timeout)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            logger.info(
                "overpass query %.4f,%.4f,%.4f,%.4f (attempt %s)",
                south, west, north, east, attempt,
            )
            try:
                response = httpx.post(
                    settings.OVERPASS_URL,
                    data={"data": query},
                    headers={"User-Agent": settings.USER_AGENT},
                    # a little longer than the query's own timeout, so the
                    # server gets to report a timeout before we give up.
                    timeout=timeout + 15,
                    follow_redirects=True,
                )
            except httpx.TimeoutException as exc:
                raise OverpassError(
                    "OpenStreetMap took too long to answer. Try again later."
                ) from exc
            except httpx.HTTPError as exc:
                raise OverpassError(
                    "Could not reach OpenStreetMap's place service."
                ) from exc

            if response.status_code in RETRY_STATUSES:
                if attempt == MAX_ATTEMPTS:
                    raise OverpassError(
                        "OpenStreetMap's place service is busy right now "
                        f"(HTTP {response.status_code}). Try again in a few minutes."
                    )
                wait = _wait_before_retry(response, attempt)
                logger.warning(
                    "overpass HTTP %s, retrying in %.0f s",
                    response.status_code, wait,
                )
                sleep(wait)
                continue

            if response.is_error:
                raise OverpassError(
                    f"OpenStreetMap's place service failed (HTTP {response.status_code})."
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise OverpassError(
                    "OpenStreetMap returned a response Pina couldn't read."
                ) from exc

            # a server-side timeout or out-of-memory can still be HTTP 200,
            # with partial results and a remark. treat it as a failure.
            remark = payload.get("remark") if isinstance(payload, dict) else None
            if isinstance(remark, str) and "error" in remark.lower():
                logger.warning("overpass remark: %s", remark)
                raise OverpassError(
                    "OpenStreetMap couldn't finish the search for this area "
                    "in time. Try again later."
                )

            elements = payload.get("elements") if isinstance(payload, dict) else None
            if not isinstance(elements, list):
                raise OverpassError(
                    "OpenStreetMap returned a response Pina couldn't read."
                )
            return elements

        # unreachable: the loop either returns or raises.
        raise OverpassError("OpenStreetMap's place service is busy right now.")


_client = OverpassClient()


def get_overpass() -> OverpassClient:
    """the shared client. tests pass a fake to sync_destination instead."""
    return _client
