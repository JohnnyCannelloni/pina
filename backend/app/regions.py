"""Deciding which OpenStreetMap extract covers a destination.

READ THIS SECOND, after migration 0005.

**The problem this solves.** Geofabrik publishes OSM as a tree of regions —
continents, countries, and for some countries their states or provinces. Picking
the wrong level is expensive:

    New York City -> north-america/us              11,706 MB
                  -> north-america/us/new-york        476 MB   (25x smaller)
    Venice        -> europe/italy                   2,149 MB
                  -> europe/italy/nord-est            598 MB

The previous version of this file was a hand-written table of ~80 country codes.
It could not express sub-regions at all, so adding New York meant downloading
the entire United States.

**How this version works.** Geofabrik publishes a machine-readable index of all
554 regions, with geometry, at 3.6 MB. We cache it and pick the **smallest
region whose bounding box contains the destination**. No hand-maintained table,
every region reachable, and nothing to drift when Geofabrik reorganises.

**Why real point-in-polygon, and not a bounding-box test.** A bounding box was
tried first, on the theory that "smallest box containing the point" would pick
correctly for a city well inside a region. It does not, and the failures are
not edge cases — measured:

    New York City -> us/new-jersey   (New Jersey is a thin diagonal state
                                      whose bounding box covers Manhattan)
    Lisbon        -> spain           (Spain's box reaches past Portugal)

Two of five test cities resolved to the wrong country or state. A rectangle
around an irregular shape is simply not that shape. So the geometry in the index
is used properly: a crossing-number test against the real polygons, with the
bounding box kept only as a cheap pre-filter. No dependency — the algorithm is
`_point_in_ring` below, about fifteen lines.

An alternative considered and rejected: matching Geofabrik's ISO 3166-2 codes
against Nominatim's. Exact where it works, but **only 71 of 554 regions carry
those codes** — verified. `US-NY` resolves; `IT-34`, `PT-11` and `JP-13` do
not. It would have fixed the United States and nowhere else.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: Geofabrik's region index, including geometry. 3.6 MB.
INDEX_URL = "https://download.geofabrik.de/index-v1.json"

#: How long a cached index is trusted. Regions are added and reorganised rarely,
#: so a month is ample and avoids a 3.6 MB fetch on every lookup.
INDEX_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _point_in_ring(
    latitude: float, longitude: float, ring: list
) -> bool:
    """Crossing-number (ray casting) test: is the point inside this ring?

    Cast a ray east from the point and count how many edges it crosses. Odd
    means inside. The two subtleties that make or break an implementation:

    - `(y1 > latitude) != (y2 > latitude)` treats each edge as half-open in y,
      so a ray passing exactly through a shared vertex counts once rather than
      twice or zero times. Writing it with `>=` on both sides is the classic
      bug, and it makes points on horizontal alignment with a vertex flip
      unpredictably.
    - The x comparison interpolates the edge at the point's latitude rather
      than comparing to a vertex, so slanted edges are handled correctly.

    GeoJSON coordinates are [longitude, latitude] — the opposite order from
    everything else in this project.
    """
    inside = False
    count = len(ring)
    for index in range(count):
        longitude1, latitude1 = ring[index][0], ring[index][1]
        longitude2, latitude2 = ring[index - 1][0], ring[index - 1][1]

        if (latitude1 > latitude) != (latitude2 > latitude):
            crossing_longitude = longitude1 + (latitude - latitude1) / (
                latitude2 - latitude1
            ) * (longitude2 - longitude1)
            if longitude < crossing_longitude:
                inside = not inside
    return inside


@dataclass(frozen=True)
class Region:
    """One Geofabrik region, with its real geometry and a bounding box."""

    #: Geofabrik path, e.g. "us/new-york". Note this is NOT the URL path —
    #: see `pbf_url`, which the index gives us directly.
    id: str
    name: str
    south: float
    west: float
    north: float
    east: float
    #: Direct download URL, taken from the index rather than constructed. The
    #: id and the URL path differ ("us/new-york" lives under north-america/),
    #: which is exactly the sort of thing not to guess at.
    pbf_url: str
    #: Polygons as [[exterior_ring, *interior_rings], ...]. Kept because a
    #: bounding box is not accurate enough — see the note at the top of the file.
    polygons: tuple = ()

    @property
    def area(self) -> float:
        """Bounding-box area in square degrees — used to pick the smallest.

        Not a real area (a degree of longitude shrinks towards the poles), but
        for comparing nested regions at similar latitudes it orders identically
        and needs no trigonometry. Only ever used to rank regions that have
        *already* passed the polygon test, so its inaccuracy cannot pick a
        region that does not contain the point.
        """
        return (self.north - self.south) * (self.east - self.west)

    def in_bounds(self, latitude: float, longitude: float) -> bool:
        """Cheap rejection test, used before the polygon test."""
        return (
            self.south <= latitude <= self.north
            and self.west <= longitude <= self.east
        )

    def contains(self, latitude: float, longitude: float) -> bool:
        """True if the point is genuinely inside this region's shape.

        Bounding box first: it rejects the overwhelming majority of the 554
        regions in a couple of float comparisons, so the expensive test only
        runs on a handful.

        Then, per polygon: inside the exterior ring and not inside any hole.
        Holes matter for real administrative geometry — an enclave, or a state
        with a lake excluded.
        """
        if not self.in_bounds(latitude, longitude):
            return False

        for rings in self.polygons:
            if not rings:
                continue
            exterior, *holes = rings
            if not _point_in_ring(latitude, longitude, exterior):
                continue
            if any(_point_in_ring(latitude, longitude, hole) for hole in holes):
                continue
            return True
        return False


class UnsupportedRegion(LookupError):
    """No Geofabrik region covers this point, or the index is unavailable.

    Raised rather than returning None so a caller cannot mistake "we have no
    map data for here" for "there is nothing here" — a silent empty map is the
    failure mode this whole feature has been fighting.
    """


def _index_path() -> Path:
    directory = Path(settings.EXTRACT_CACHE_DIR).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "geofabrik-index.json"


def _fetch_index(path: Path) -> None:
    """Download the region index to `path`."""
    logger.info("fetching Geofabrik region index")
    try:
        response = httpx.get(
            INDEX_URL,
            headers={"User-Agent": settings.USER_AGENT},
            timeout=60.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise UnsupportedRegion(
            "Could not reach Geofabrik to list the available map regions."
        ) from exc

    # Written to a temporary name and moved into place, so an interrupted
    # download cannot leave a truncated file that later parses as valid-ish
    # JSON or fails confusingly. Same trick as the .part suffix in extract.py.
    staging = path.with_suffix(".part")
    staging.write_bytes(response.content)
    staging.replace(path)


def _bounds_of(geometry: dict) -> tuple[float, float, float, float] | None:
    """Bounding box of a GeoJSON Polygon or MultiPolygon.

    Walks the coordinate nesting rather than assuming a depth, because the index
    mixes Polygon and MultiPolygon and a wrong assumption would silently produce
    a box of the wrong shape.
    """
    south = west = float("inf")
    north = east = float("-inf")
    found = False

    def walk(node: object) -> None:
        nonlocal south, west, north, east, found
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and isinstance(node[0], (int, float))
            and isinstance(node[1], (int, float))
        ):
            longitude, latitude = float(node[0]), float(node[1])
            south, north = min(south, latitude), max(north, latitude)
            west, east = min(west, longitude), max(east, longitude)
            found = True
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(geometry.get("coordinates"))
    return (south, west, north, east) if found else None


def _polygons_of(geometry: dict) -> tuple:
    """Normalise GeoJSON Polygon / MultiPolygon to a tuple of ring-lists.

    The index mixes both types, and their nesting differs by exactly one level:

        Polygon      coordinates = [ring, hole, ...]
        MultiPolygon coordinates = [[ring, hole, ...], ...]

    Flattening that difference here means `Region.contains` has one shape to
    handle. Getting it wrong would treat a Polygon's holes as separate polygons,
    which would make points inside a hole test as *inside* the region.
    """
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return ()

    kind = geometry.get("type")
    if kind == "Polygon":
        return (coordinates,)
    if kind == "MultiPolygon":
        return tuple(coordinates)
    return ()


# Parsed index, kept for the life of the process. The file is 3.6 MB of JSON;
# re-parsing it per request would be wasteful and it never changes underneath us.
_regions: list[Region] | None = None


def load_index(force: bool = False) -> list[Region]:
    """Every Geofabrik region with a usable bounding box.

    Cached in memory, and on disk for INDEX_MAX_AGE_SECONDS.
    """
    global _regions
    if _regions is not None and not force:
        return _regions

    path = _index_path()
    stale = (
        not path.exists()
        or time.time() - path.stat().st_mtime > INDEX_MAX_AGE_SECONDS
    )
    if stale or force:
        _fetch_index(path)

    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise UnsupportedRegion(
            "The cached Geofabrik region index could not be read."
        ) from exc

    regions: list[Region] = []
    for feature in payload.get("features", []):
        properties = feature.get("properties") or {}
        geometry = feature.get("geometry") or {}
        region_id = properties.get("id")
        pbf_url = (properties.get("urls") or {}).get("pbf")

        # A region with no id, no .pbf, or no geometry cannot be used. Skipped
        # quietly: the index legitimately contains a few such entries, and one
        # of them should not break every lookup.
        if not region_id or not pbf_url or not geometry:
            continue

        bounds = _bounds_of(geometry)
        if bounds is None:
            continue

        south, west, north, east = bounds
        regions.append(
            Region(
                id=region_id,
                name=properties.get("name") or region_id,
                south=south,
                west=west,
                north=north,
                east=east,
                pbf_url=pbf_url,
                polygons=_polygons_of(geometry),
            )
        )

    if not regions:
        raise UnsupportedRegion(
            "The Geofabrik region index contained no usable regions."
        )

    logger.info("loaded %s Geofabrik regions", len(regions))
    _regions = regions
    return regions


def resolve_region(latitude: float, longitude: float) -> Region:
    """The smallest Geofabrik region containing a point.

    "Smallest" is the whole point: New York City sits inside both
    `north-america/us` (11.7 GB) and `north-america/us/new-york` (476 MB), and
    we want the latter. Sorting candidates by bounding-box area and taking the
    first gives that, and automatically falls back to the country when a country
    has no sub-regions.
    """
    candidates = [
        region
        for region in load_index()
        if region.contains(latitude, longitude)
    ]
    if not candidates:
        raise UnsupportedRegion(
            f"No OpenStreetMap region covers {latitude:.4f}, {longitude:.4f}. "
            "You can still add places here by hand."
        )

    candidates.sort(key=lambda region: region.area)
    chosen = candidates[0]
    logger.info(
        "resolved %.4f,%.4f -> %s (from %s candidates)",
        latitude,
        longitude,
        chosen.id,
        len(candidates),
    )
    return chosen


def find_region(region_id: str) -> Region:
    """Look up a region by its Geofabrik id, for the CLI and for coverage."""
    for region in load_index():
        if region.id == region_id:
            return region
    raise UnsupportedRegion(f"No Geofabrik region with id '{region_id}'.")


def candidates_for(latitude: float, longitude: float) -> list[Region]:
    """Every region containing a point, smallest first.

    Used by `fill_places --list-regions --near`, so a human can see what was
    chosen *and what it was chosen over* — which is how you would notice a
    border city picking the wrong side.
    """
    regions = [
        region for region in load_index() if region.contains(latitude, longitude)
    ]
    regions.sort(key=lambda region: region.area)
    return regions
