"""Turning a typed city name into a destination, via Nominatim.

One of two outside services Pina uses, and the split between them is about what
each is *allowed* to be asked for:

    Nominatim  "where is Lisbon?" — one lookup, a few results, on demand.
               Its usage policy forbids bulk or heavy use and asks for at most
               one request per second. We call it once per destination a human
               adds, which is comfortably inside that.

    Geofabrik  "every restaurant in Portugal" — bulk data.
               A static file download, with no query engine to overload. See
               app/extract.py.

Using Nominatim for the second job would get us blocked, and deservedly. An
earlier version of Pina used the Overpass API there instead; it was the right
*kind* of service but its public instances are busy by default, and the feature
built on it did not work. The bulk job moved to static files; this one stayed.

Measured: HTTP 200 in 0.45s with accurate results. Answering one geocode is
cheap, which is why this is the half that was never the problem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

#: Nominatim's policy requires a User-Agent identifying the application.
#: Requests with a default library agent get blocked.
USER_AGENT = "Pina/0.1 (single-user travel planner; local development)"

#: How many candidates to offer. "Venice" matches Venice, Italy and Venice,
#: Florida (and Venice Beach), so the user picks — guessing the first result
#: silently is how you end up planning a trip to the wrong continent.
MAX_RESULTS = 5


@dataclass(frozen=True)
class GeocodeResult:
    """A candidate destination.

    Already in *our* axis order (south/west/north/east), converted from
    Nominatim's, so nothing downstream has to remember the difference.
    """

    #: Full label from Nominatim, e.g. "Venice, Veneto, Italy".
    display_name: str
    #: Short label for the destination's name, e.g. "Venice".
    name: str
    latitude: float
    longitude: float
    south: float
    west: float
    north: float
    east: float
    #: "relation/44915" — identity, used to stop one city being added twice.
    osm_ref: str
    #: ISO 3166-1 alpha-2, e.g. "pt". This is what decides which Geofabrik
    #: extract gets downloaded — see app/regions.py. None when Nominatim does
    #: not say, in which case the destination works but cannot auto-fill.
    country_code: str | None


class GeocodeError(RuntimeError):
    """Nominatim could not be reached, or did not answer usefully.

    A distinct type so routes.py can map exactly this to a 503 and let genuine
    bugs in our own code keep surfacing as 500s.
    """


def parse_results(payload: object) -> list[GeocodeResult]:
    """Convert Nominatim's JSON into GeocodeResults.

    Split out from the HTTP call so it can be tested against a literal payload,
    and so the two awkward parts of Nominatim's format are isolated in one
    place:

    1. **`boundingbox` is a list of STRINGS**, not numbers:
           ["45.4097", "45.4571", "12.2890", "12.3615"]
    2. **Its order is [min_lat, max_lat, min_lon, max_lon]** — that is
       south, NORTH, west, east. Every other bounding box in this project is
       south/west/north/east. Reading it positionally without noticing gives
       you a box with north and west swapped, which passes a careless eye and
       then fails ck_destinations_bbox_ordered in the database. Converting here,
       once, is why nothing else has to know.
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
            # Note the destructuring order — it is Nominatim's, not ours.
            min_lat, max_lat, min_lon, max_lon = (float(value) for value in box)
            latitude = float(item["lat"])
            longitude = float(item["lon"])
        except (KeyError, TypeError, ValueError):
            # A malformed entry is skipped rather than failing the whole
            # search; one bad candidate should not cost the user the others.
            continue

        osm_type = item.get("osm_type")
        osm_id = item.get("osm_id")
        if not osm_type or osm_id is None:
            continue

        display_name = str(item.get("display_name") or "").strip()
        if not display_name:
            continue

        # With addressdetails=1 Nominatim nests this under "address". Lowercased
        # because the Geofabrik lookup keys on lowercase, and Nominatim is not
        # perfectly consistent about case.
        address = item.get("address")
        country_code = None
        if isinstance(address, dict):
            raw = address.get("country_code")
            if isinstance(raw, str) and raw.strip():
                country_code = raw.strip().lower()

        results.append(
            GeocodeResult(
                display_name=display_name,
                # The first comma-separated part is the place itself; the rest
                # is region and country. "Venice, Veneto, Italy" -> "Venice".
                name=display_name.split(",")[0].strip(),
                latitude=latitude,
                longitude=longitude,
                # Mapped into our order here.
                south=min_lat,
                west=min_lon,
                north=max_lat,
                east=max_lon,
                osm_ref=f"{osm_type}/{osm_id}",
                country_code=country_code,
            )
        )

    return results


class NominatimClient:
    """Looks up places by name.

    No cache, deliberately: this is called once when a human adds a destination,
    so there is nothing to amortise. Overpass gets a cache because the map hits
    it repeatedly; this does not.
    """

    def search(self, query: str) -> list[GeocodeResult]:
        """Find candidate destinations for a typed name.

        Raises GeocodeError if the service cannot be reached.
        """
        cleaned = query.strip()
        if not cleaned:
            return []

        logger.info("geocoding %r", cleaned)

        try:
            response = httpx.get(
                settings.NOMINATIM_URL,
                params={
                    "q": cleaned,
                    "format": "jsonv2",
                    "limit": MAX_RESULTS,
                    # Needed for address.country_code, which decides which
                    # Geofabrik extract to download. Without it the lookup
                    # succeeds and the destination then cannot auto-fill — a
                    # failure two steps removed from its cause.
                    "addressdetails": 1,
                },
                headers={"User-Agent": USER_AGENT},
                timeout=settings.NOMINATIM_TIMEOUT_SECONDS,
                # Nominatim redirects http -> https; following keeps a
                # misconfigured URL working instead of silently returning a 301
                # body that fails to parse.
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
    """FastAPI dependency returning the shared client.

    The same testing seam as get_db and get_overpass_client: conftest.py swaps
    this for a fake, which is how the suite stays off the network.
    """
    return _client
