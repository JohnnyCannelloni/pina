"""geocoding through nominatim: city names, and areas inside a destination.

nominatim's usage policy allows light, on-demand lookups only, so it's called
once per added destination or ask pina area. bulk place data comes from
geofabrik instead (app/extract.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: nominatim requires a user agent that identifies the app.
USER_AGENT = "Pina/0.1 (single-user travel planner; local development)"

#: several candidates, so the user can pick between e.g. venice, italy and
#: venice, florida.
MAX_RESULTS = 5


@dataclass(frozen=True)
class GeocodeResult:
    """a candidate place, with its box already in south/west/north/east order."""

    #: e.g. "Venice, Veneto, Italy".
    display_name: str
    #: e.g. "Venice".
    name: str
    latitude: float
    longitude: float
    south: float
    west: float
    north: float
    east: float
    #: e.g. "relation/44915". stops the same city being added twice.
    osm_ref: str
    #: iso 3166-1 alpha-2, e.g. "pt". none when nominatim doesn't say.
    country_code: str | None
    #: nominatim's 0-1 notability score. ask pina picks areas by this, since
    #: the result order is unreliable for bare names (a "rialto" service road
    #: outranks the rialto market).
    importance: float = 0.0


class GeocodeError(RuntimeError):
    """nominatim couldn't be reached or gave an unusable answer. maps to a 503."""


def parse_results(payload: object) -> list[GeocodeResult]:
    """convert nominatim's json into GeocodeResults.

    note that `boundingbox` is a list of strings in [south, north, west, east]
    order, unlike every other box in this project.
    """
    if not isinstance(payload, list):
        raise GeocodeError("Unexpected response from the geocoder.")

    results: list[GeocodeResult] = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        box = item.get("boundingbox")
        if not isinstance(box, list) or len(box) != 4:
            continue

        try:
            # nominatim's order, not ours.
            min_lat, max_lat, min_lon, max_lon = (float(value) for value in box)
            latitude = float(item["lat"])
            longitude = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            # skip a malformed entry rather than failing the whole search.
            continue

        try:
            importance = float(item.get("importance") or 0.0)
        except (TypeError, ValueError):
            importance = 0.0

        osm_type = item.get("osm_type")
        osm_id = item.get("osm_id")
        if not osm_type or osm_id is None:
            continue

        display_name = str(item.get("display_name") or "").strip()
        if not display_name:
            continue

        # nested under "address" with addressdetails=1. lowercased for the
        # geofabrik lookup.
        address = item.get("address")
        country_code = None
        if isinstance(address, dict):
            raw = address.get("country_code")
            if isinstance(raw, str) and raw.strip():
                country_code = raw.strip().lower()

        results.append(
            GeocodeResult(
                display_name=display_name,
                # "Venice, Veneto, Italy" -> "Venice".
                name=display_name.split(",")[0].strip(),
                latitude=latitude,
                longitude=longitude,
                # converted to our order.
                south=min_lat,
                west=min_lon,
                north=max_lat,
                east=max_lon,
                osm_ref=f"{osm_type}/{osm_id}",
                country_code=country_code,
                importance=importance,
            )
        )

    return results


class NominatimClient:
    """looks up places by name. no cache: it's called rarely."""

    def search(
        self,
        query: str,
        viewbox: tuple[float, float, float, float] | None = None,
        limit: int = MAX_RESULTS,
    ) -> list[GeocodeResult]:
        """find candidates for a typed name.

        `viewbox` is (south, west, north, east). when given, only results
        inside it are returned, so "centro" means this city's centre.

        raises GeocodeError if the service can't be reached.
        """
        cleaned = query.strip()
        if not cleaned:
            return []

        logger.info("geocoding %r", cleaned)

        params: dict[str, str | int] = {
            "q": cleaned,
            "format": "jsonv2",
            "limit": limit,
            # needed for country_code, which picks the region to download.
            "addressdetails": 1,
        }
        if viewbox is not None:
            south, west, north, east = viewbox
            # nominatim wants lon,lat order. bounded=1 makes it a hard filter.
            params["viewbox"] = f"{west},{north},{east},{south}"
            params["bounded"] = 1

        try:
            response = httpx.get(
                settings.NOMINATIM_URL,
                params=params,
                headers={"User-Agent": USER_AGENT},
                timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
                # nominatim redirects http to https.
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise GeocodeError(
                "The place lookup took too long. Try again."
            ) from exc
        except httpx.HTTPError as exc:
            raise GeocodeError(
                "Could not reach the place lookup service."
            ) from exc

        if response.status_code == 429:
            raise GeocodeError(
                "Too many lookups too quickly. Wait a moment and try again."
            )
        if response.is_error:
            raise GeocodeError(
                f"The place lookup failed (HTTP {response.status_code})."
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise GeocodeError(
                "The place lookup returned a response we could not read."
            ) from exc

        return parse_results(payload)


_client = NominatimClient()


def get_geocoder() -> NominatimClient:
    """fastapi dependency. tests swap this for a fake."""
    return _client
