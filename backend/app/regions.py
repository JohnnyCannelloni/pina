"""pick the geofabrik extract that covers a destination.

uses geofabrik's region index (with geometry) and chooses the smallest region
whose polygon contains the point, e.g. us/new-york (476 mb) rather than us
(11.7 gb). a plain bounding-box test isn't enough: it put new york city in
new jersey and lisbon in spain.
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

#: geofabrik's region index, including geometry (~3.6 mb).
INDEX_URL = "https://download.geofabrik.de/index-v1.json"

#: regions change rarely, so the cached index is trusted for a month.
INDEX_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _point_in_ring(
    latitude: float, longitude: float, ring: list
) -> bool:
    """ray-casting point-in-polygon test.

    edges are half-open in latitude so a ray through a shared vertex counts
    once. ring coordinates are geojson [longitude, latitude].
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
    """one geofabrik region with its geometry and bounding box."""

    #: e.g. "us/new-york". not the url path, see pbf_url.
    id: str
    name: str
    south: float
    west: float
    north: float
    east: float
    #: taken from the index; the url path differs from the id.
    pbf_url: str
    #: [[exterior_ring, *holes], ...]
    polygons: tuple = ()

    @property
    def area(self) -> float:
        """bounding-box area in square degrees, only used to rank regions
        that already contain the point."""
        return (self.north - self.south) * (self.east - self.west)

    def in_bounds(self, latitude: float, longitude: float) -> bool:
        """cheap pre-filter before the polygon test."""
        return (
            self.south <= latitude <= self.north
            and self.west <= longitude <= self.east
        )

    def contains(self, latitude: float, longitude: float) -> bool:
        """true if the point is inside one of the polygons and not in a hole."""
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
    """no region covers this point, or the index is unavailable."""


def _index_path() -> Path:
    directory = Path(settings.EXTRACT_CACHE_DIR).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "geofabrik-index.json"


def _fetch_index(path: Path) -> None:
    """download the region index to `path`."""
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

    # write to a temp file and rename, so an interrupted download leaves nothing behind.
    staging = path.with_suffix(".part")
    staging.write_bytes(response.content)
    staging.replace(path)


def _bounds_of(geometry: dict) -> tuple[float, float, float, float] | None:
    """bounding box of a geojson polygon or multipolygon, at any nesting depth."""
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
    """normalise polygon / multipolygon into a tuple of ring lists.

        Polygon      coordinates = [ring, hole, ...]
        MultiPolygon coordinates = [[ring, hole, ...], ...]
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


# parsed once per process.
_regions: list[Region] | None = None


def load_index(force: bool = False) -> list[Region]:
    """every usable geofabrik region. cached in memory and on disk."""
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

        # the index has a few unusable entries; skip them.
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
    """the smallest geofabrik region containing a point.

    falls back to the country when it has no sub-regions.
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
    """look up a region by its geofabrik id."""
    for region in load_index():
        if region.id == region_id:
            return region
    raise UnsupportedRegion(f"No Geofabrik region with id '{region_id}'.")


def candidates_for(latitude: float, longitude: float) -> list[Region]:
    """every region containing a point, smallest first. used by the cli."""
    regions = [
        region for region in load_index() if region.contains(latitude, longitude)
    ]
    regions.sort(key=lambda region: region.area)
    return regions
