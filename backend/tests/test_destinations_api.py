"""geocoding and adding, polling, retrying and deleting destinations."""

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.geocode import GeocodeError, GeocodeResult, parse_results
from app.models import Destination, OsmPlace, Place, SyncState
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


# --- parsing nominatim's format ----------------------------------------------


def test_parse_results_converts_nominatims_bbox_order() -> None:
    """nominatim's boundingbox is strings in [s, n, w, e] order; we convert it."""
    results = parse_results(
        [
            {
                "display_name": "Venice, Veneto, Italy",
                "lat": "45.4371",
                "lon": "12.3326",
                # min_lat, max_lat, min_lon, max_lon, as strings.
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
    # ordered the way the database requires.
    assert venice.north > venice.south
    assert venice.east > venice.west


def test_parse_results_skips_malformed_entries() -> None:
    """one bad candidate doesn't drop the good ones."""
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


# --- GET /api/geocode --------------------------------------------------------


def test_geocode_returns_candidates(
    client: TestClient, fake_geocoder: FakeGeocoder
) -> None:
    """several candidates come back, so the user picks."""
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
    """a geocoder outage is a readable 503."""
    fake_geocoder.error = GeocodeError("Could not reach the place lookup service.")

    response = client.get("/api/geocode", params={"q": "Lisbon"})
    assert response.status_code == 503, response.text
    assert "place lookup" in response.json()["detail"]


# --- POST /api/trips/{id}/destinations ---------------------------------------


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
    """adding a city returns straight away, pending, with a sync scheduled."""
    response = client.post(
        f"/api/trips/{venice.trip_id}/destinations", json=_create_body(LISBON)
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["name"] == "Lisbon"
    assert body["sync_state"] == SyncState.PENDING
    assert body["osm_place_count"] == 0
    # the box is stored as sent; it's what the overpass query will use.
    assert (body["south"], body["west"], body["north"], body["east"]) == (
        38.69, -9.23, 38.80, -9.09
    )
    # region tracking is gone (migration 0007).
    assert "region" not in body and "extract_state" not in body

    # the sync was scheduled for this destination.
    assert sync_runner.started == [body["id"]]


def test_create_destination_clamps_an_oversized_area(
    client: TestClient, venice: Destination, sync_runner: RecordingSyncRunner
) -> None:
    """a continent-sized box is clamped."""
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
    """adding the same city twice is a 409."""
    body = _create_body(LISBON)
    first = client.post(f"/api/trips/{venice.trip_id}/destinations", json=body)
    assert first.status_code == 201, first.text

    second = client.post(f"/api/trips/{venice.trip_id}/destinations", json=body)
    assert second.status_code == 409, second.text
    assert "already a destination" in second.json()["detail"]


def test_create_destination_rejects_an_inverted_box(
    client: TestClient, venice: Destination, sync_runner: RecordingSyncRunner
) -> None:
    """north below south is a 422."""
    body = _create_body(LISBON) | {"north": 38.60}  # below south=38.69

    response = client.post(f"/api/trips/{venice.trip_id}/destinations", json=body)
    assert response.status_code == 422, response.text


def test_create_destination_404s_for_an_unknown_trip(
    client: TestClient, sync_runner: RecordingSyncRunner
) -> None:
    response = client.post("/api/trips/9999/destinations", json=_create_body(LISBON))
    assert response.status_code == 404, response.text


# --- GET /api/destinations/{id} and POST .../sync ----------------------------


def test_get_destination_reports_cache_status(
    client: TestClient, venice: Destination
) -> None:
    """the detail the ui polls while syncing."""
    response = client.get(f"/api/destinations/{venice.id}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["name"] == "Venice"
    assert body["sync_state"] == SyncState.PENDING
    assert "osm_place_count" in body
    # the seeded destination's area comes from migration 0003.
    assert body["north"] > body["south"]
    assert body["east"] > body["west"]


def test_resync_reschedules_the_import(
    client: TestClient,
    venice: Destination,
    sync_runner: RecordingSyncRunner,
) -> None:
    """retry resets the destination to pending and schedules a new run."""
    response = client.post(f"/api/destinations/{venice.id}/sync")
    assert response.status_code == 200, response.text
    assert response.json()["sync_state"] == SyncState.PENDING
    assert response.json()["sync_error"] is None
    assert sync_runner.started == [venice.id]


def test_any_location_gets_a_sync(
    client: TestClient,
    venice: Destination,
    sync_runner: RecordingSyncRunner,
) -> None:
    """overpass covers the whole world, so even the middle of the ocean syncs.

    (with geofabrik regions this point used to be "unsupported".)
    """
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
    assert body["sync_state"] == SyncState.PENDING
    assert body["sync_error"] is None
    assert sync_runner.started == [body["id"]]


# --- deleting ----------------------------------------------------------------


def test_delete_a_destination(client: TestClient, venice: Destination) -> None:
    """gone from its url and from the trip list."""
    response = client.delete(f"/api/destinations/{venice.id}")
    assert response.status_code == 204, response.text
    assert response.content == b""

    assert client.get(f"/api/destinations/{venice.id}").status_code == 404
    trips = client.get("/api/trips").json()
    assert all(d["id"] != venice.id for t in trips for d in t["destinations"])


def test_deleting_a_destination_deletes_its_saved_places(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Caffè Florian", "category": "cafe"},
    )

    client.delete(f"/api/destinations/{venice.id}")

    remaining = db_session.scalar(
        select(func.count()).select_from(Place).where(Place.destination_id == venice.id)
    )
    assert remaining == 0


def test_deleting_a_destination_keeps_the_map_cache(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """the shared osm cache survives a delete."""
    db_session.add(
        OsmPlace(
            source_ref="node/42",
            name="Bar all'Arco",
            category="cafe",
            latitude=venice.latitude,
            longitude=venice.longitude,
        )
    )
    db_session.commit()

    client.delete(f"/api/destinations/{venice.id}")

    assert db_session.scalar(
        select(OsmPlace).where(OsmPlace.source_ref == "node/42")
    ) is not None


def test_delete_an_unknown_destination_is_a_404(client: TestClient) -> None:
    assert client.delete("/api/destinations/999999").status_code == 404
