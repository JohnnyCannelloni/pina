"""Tests for geocoding and adding destinations.

This is what makes Pina a planner for *any* city rather than a Venice demo. No
test touches the network: the Nominatim client and the sync runner are both
dependency overrides from conftest.py.
"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.geocode import GeocodeError, GeocodeResult, parse_results
from app.models import Destination, ExtractState, OsmExtract, SyncState
from app.sync import MAX_DESTINATION_SPAN_DEGREES
from tests.conftest import FakeGeocoder, RecordingSyncRunner

LISBON = GeocodeResult(
    display_name="Lisbon, Portugal",
    name="Lisbon",
    latitude=38.7223,
    longitude=-9.1393,
    south=38.69,
    west=-9.23,
    north=38.80,
    east=-9.09,
    osm_ref="relation/5400890",
    country_code="pt",
)


# --- parsing Nominatim's awkward format ------------------------------------


def test_parse_results_converts_nominatims_bbox_order() -> None:
    """Nominatim's boundingbox is strings, in [S, N, W, E] order.

    Everything else in this project uses south/west/north/east. Reading
    Nominatim's array positionally without noticing gives a box with north and
    west swapped — which looks plausible and then violates
    ck_destinations_bbox_ordered in the database. This test pins the conversion.
    """
    results = parse_results(
        [
            {
                "display_name": "Venice, Veneto, Italy",
                "lat": "45.4371",
                "lon": "12.3326",
                # min_lat, max_lat, min_lon, max_lon — as strings.
                "boundingbox": ["45.4097", "45.4571", "12.2890", "12.3615"],
                "osm_type": "relation",
                "osm_id": 44915,
                "address": {"country_code": "it"},
            }
        ]
    )

    assert len(results) == 1
    venice = results[0]
    assert venice.name == "Venice"
    assert venice.south == 45.4097
    assert venice.north == 45.4571
    assert venice.west == 12.2890
    assert venice.east == 12.3615
    assert venice.osm_ref == "relation/44915"
    assert venice.country_code == "it"
    # Ordering holds, which is what the database will insist on.
    assert venice.north > venice.south
    assert venice.east > venice.west


def test_parse_results_skips_malformed_entries() -> None:
    """One bad candidate does not cost the user the good ones."""
    results = parse_results(
        [
            {"display_name": "No bbox", "lat": "1", "lon": "2", "osm_type": "node", "osm_id": 1},
            {
                "display_name": "Good, Place",
                "lat": "45.0",
                "lon": "12.0",
                "boundingbox": ["44.9", "45.1", "11.9", "12.1"],
                "osm_type": "relation",
                "osm_id": 7,
            },
        ]
    )
    assert [result.name for result in results] == ["Good"]


# --- GET /api/geocode ------------------------------------------------------


def test_geocode_returns_candidates(
    client: TestClient, fake_geocoder: FakeGeocoder
) -> None:
    """Several candidates come back, so the user picks.

    "Venice" is a real place in Italy, Florida and California. Returning one
    best guess is how a trip ends up on the wrong continent.
    """
    fake_geocoder.results = [LISBON]

    response = client.get("/api/geocode", params={"q": "Lisbon"})
    assert response.status_code == 200, response.text

    body = response.json()
    assert len(body) == 1
    assert body[0]["display_name"] == "Lisbon, Portugal"
    assert body[0]["osm_ref"] == "relation/5400890"
    assert fake_geocoder.queries == ["Lisbon"]


def test_geocode_503s_when_the_service_is_unavailable(
    client: TestClient, fake_geocoder: FakeGeocoder
) -> None:
    """A geocoder outage is a 503 with a readable message, not a 500."""
    fake_geocoder.error = GeocodeError("Could not reach the place lookup service.")

    response = client.get("/api/geocode", params={"q": "Lisbon"})
    assert response.status_code == 503, response.text
    assert "place lookup" in response.json()["detail"]


# --- POST /api/trips/{id}/destinations -------------------------------------


def _create_body(result: GeocodeResult) -> dict:
    return {
        "name": result.name,
        "latitude": result.latitude,
        "longitude": result.longitude,
        "south": result.south,
        "west": result.west,
        "north": result.north,
        "east": result.east,
        "osm_ref": result.osm_ref,
        "country_code": result.country_code,
    }


def test_create_destination_starts_a_sync(
    client: TestClient,
    venice: Destination,
    sync_runner: RecordingSyncRunner,
) -> None:
    """Adding a city returns immediately and schedules the fetch.

    `sync_state='pending'` plus a resolved `region` is the contract the UI
    relies on: it can say *which* country it is about to download before any
    bytes move.
    """
    response = client.post(
        f"/api/trips/{venice.trip_id}/destinations", json=_create_body(LISBON)
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["name"] == "Lisbon"
    assert body["sync_state"] == SyncState.PENDING
    assert body["osm_place_count"] == 0
    # Resolved from the coordinates against Geofabrik's real index, so an
    # unsupported location is reported at creation rather than minutes later.
    # Note the id is Geofabrik's own ("portugal"), not a continent-prefixed
    # path — see the normalisation step in migration 0005.
    assert body["region"] == "portugal"

    # The sync was handed to the background, for this destination.
    assert sync_runner.started == [body["id"]]


def test_create_destination_clamps_an_oversized_area(
    client: TestClient, venice: Destination, sync_runner: RecordingSyncRunner
) -> None:
    """A continent-sized bbox is cut down before anything is cached."""
    tokyo = GeocodeResult(
        display_name="Tokyo, Japan",
        name="Tokyo",
        latitude=35.6762,
        longitude=139.6503,
        south=35.2,
        west=138.9,
        north=36.2,
        east=140.5,
        osm_ref="relation/1543125",
        country_code="jp",
    )

    response = client.post(
        f"/api/trips/{venice.trip_id}/destinations", json=_create_body(tokyo)
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["north"] - body["south"] <= MAX_DESTINATION_SPAN_DEGREES + 1e-9
    assert body["east"] - body["west"] <= MAX_DESTINATION_SPAN_DEGREES + 1e-9


def test_adding_the_same_city_twice_is_a_conflict(
    client: TestClient, venice: Destination, sync_runner: RecordingSyncRunner
) -> None:
    """ix_destinations_trip_osm_ref refuses a duplicate; the API says 409."""
    body = _create_body(LISBON)
    first = client.post(f"/api/trips/{venice.trip_id}/destinations", json=body)
    assert first.status_code == 201, first.text

    second = client.post(f"/api/trips/{venice.trip_id}/destinations", json=body)
    assert second.status_code == 409, second.text
    assert "already a destination" in second.json()["detail"]


def test_create_destination_rejects_an_inverted_box(
    client: TestClient, venice: Destination, sync_runner: RecordingSyncRunner
) -> None:
    """north below south is a 422 before anything is written."""
    body = _create_body(LISBON) | {"north": 38.60}  # below south=38.69

    response = client.post(f"/api/trips/{venice.trip_id}/destinations", json=body)
    assert response.status_code == 422, response.text


def test_create_destination_404s_for_an_unknown_trip(
    client: TestClient, sync_runner: RecordingSyncRunner
) -> None:
    response = client.post("/api/trips/9999/destinations", json=_create_body(LISBON))
    assert response.status_code == 404, response.text


# --- GET /api/destinations/{id} and POST .../sync --------------------------


def test_get_destination_reports_cache_status(
    client: TestClient, venice: Destination
) -> None:
    """What the UI polls while the cache fills."""
    response = client.get(f"/api/destinations/{venice.id}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["name"] == "Venice"
    assert body["sync_state"] == SyncState.PENDING
    assert "osm_place_count" in body and "region" in body
    # The seeded destination has a real area, from migration 0003's backfill.
    assert body["north"] > body["south"]
    assert body["east"] > body["west"]


def test_get_destination_reports_extract_state(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """A destination with a region reports its extract's state.

    This test exists because its absence hid a NameError: `extract_state` is
    only evaluated when the destination *has* a region, and every other test
    polled the seeded Venice, which has none. The guard meant a missing import
    produced a 500 that the whole suite stepped over. A conditional branch no
    test enters is a branch that is not tested.
    """
    venice.region = "europe/italy"
    db_session.add(
        OsmExtract(
            region="europe/italy",
            state=ExtractState.READY,
            place_count=1234,
        )
    )
    db_session.commit()

    response = client.get(f"/api/destinations/{venice.id}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["region"] == "europe/italy"
    assert body["extract_state"] == ExtractState.READY


def test_resync_reschedules_the_import(
    client: TestClient,
    venice: Destination,
    sync_runner: RecordingSyncRunner,
) -> None:
    """Retry puts the destination back to pending and schedules a fresh run.

    There is no tile state to reset any more: a cached extract is reused and a
    partial download resumes, so re-running is simply safe.
    """
    response = client.post(f"/api/destinations/{venice.id}/sync")
    assert response.status_code == 200, response.text
    assert response.json()["sync_state"] == SyncState.PENDING
    assert response.json()["sync_error"] is None
    assert sync_runner.started == [venice.id]


def test_an_unsupported_location_is_created_but_flagged(
    client: TestClient,
    venice: Destination,
    sync_runner: RecordingSyncRunner,
) -> None:
    """A country we have no extract for still gets a usable destination.

    It is marked failed with an explanation rather than sitting at 'pending'
    forever, and no import is scheduled because there is nothing to fetch. You
    can still save places there by hand.
    """
    # Mid-Pacific. Region resolution is geographic now, so "unsupported" means
    # "no Geofabrik region contains this point" rather than "unknown country
    # code" — open ocean is the honest example.
    nowhere = GeocodeResult(
        display_name="Point Nemo, Pacific Ocean",
        name="Point Nemo",
        latitude=-48.876,
        longitude=-123.393,
        south=-48.886,
        west=-123.403,
        north=-48.866,
        east=-123.383,
        osm_ref="relation/999999",
        country_code=None,
    )

    response = client.post(
        f"/api/trips/{venice.trip_id}/destinations", json=_create_body(nowhere)
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["sync_state"] == SyncState.FAILED
    assert body["region"] is None
    assert "No OpenStreetMap region covers" in body["sync_error"]
    assert sync_runner.started == []
