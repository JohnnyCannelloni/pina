"""sync_destination with a fake overpass client: pending -> ready, or failed."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import sync as sync_module
from app.models import Destination, OsmPlace, Place, SyncState
from app.overpass import MAX_SPAN_DEGREES, OverpassError
from app.sync import (
    MAX_DESTINATION_SPAN_DEGREES,
    UNEXPECTED_ERROR,
    clamp_area,
    sync_destination,
)
from tests.conftest import FakeOverpass

# inside the seeded venice box.
RIALTO = (45.4380, 12.3358)


def elements_in_venice() -> list[dict]:
    return [
        {"type": "node", "id": 1, "lat": RIALTO[0], "lon": RIALTO[1],
         "tags": {"name": "Da Mario", "amenity": "restaurant", "cuisine": "italian"}},
        {"type": "way", "id": 1, "center": {"lat": 45.4340, "lon": 12.3390},
         "tags": {"name": "Gallerie dell'Accademia", "tourism": "museum"}},
    ]


def run(destination: Destination, factory: sessionmaker[Session], fake: FakeOverpass) -> None:
    sync_destination(destination.id, session_factory=factory, overpass=fake)


def reload(db: Session, destination: Destination) -> Destination:
    db.expire_all()
    return db.get(Destination, destination.id)  # type: ignore[return-value]


def test_a_sync_fills_the_cache_and_ends_ready(
    client: TestClient,
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    # a place saved before the sync, linked by its node ref.
    db_session.add(Place(
        destination_id=venice.id, name="Da Mario", category="restaurant",
        source="osm", source_ref="node/1",
    ))
    db_session.commit()
    fake_overpass.elements = elements_in_venice()

    run(venice, sync_session_factory, fake_overpass)

    destination = reload(db_session, venice)
    assert destination.sync_state == SyncState.READY
    assert destination.sync_error is None
    assert destination.synced_at is not None
    # queried once, with the destination's own box.
    assert fake_overpass.boxes == [
        (venice.south, venice.west, venice.north, venice.east)
    ]

    refs = dict(db_session.execute(select(OsmPlace.source_ref, OsmPlace.name)).all())
    assert refs == {"node/1": "Da Mario", "way/1": "Gallerie dell'Accademia"}

    # the saved place now gets its details through the same source_ref.
    saved = client.get(f"/api/destinations/{venice.id}/places").json()
    assert saved[0]["details"]["cuisine"] == ["italian"]

    # and the museum mapped as a building outline shows on the map.
    nearby = client.get(
        f"/api/destinations/{venice.id}/nearby",
        params={"south": 45.43, "west": 12.33, "north": 45.44, "east": 12.34},
    ).json()
    assert "Gallerie dell'Accademia" in {p["name"] for p in nearby["places"]}
    assert nearby["coverage"] == "covered"


def test_an_area_already_synced_is_not_queried_again(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    venice.sync_state = SyncState.READY
    inside = Destination(
        trip_id=venice.trip_id, name="San Polo", osm_ref="relation/1",
        latitude=45.438, longitude=12.330,
        south=45.43, west=12.32, north=45.445, east=12.34,
    )
    db_session.add(inside)
    db_session.commit()

    run(inside, sync_session_factory, fake_overpass)

    assert reload(db_session, inside).sync_state == SyncState.READY
    assert fake_overpass.boxes == []


def test_a_partly_overlapping_area_is_queried(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    venice.sync_state = SyncState.READY
    across_the_edge = Destination(
        trip_id=venice.trip_id, name="Mestre", osm_ref="relation/2",
        latitude=45.49, longitude=12.24,
        south=45.45, west=12.20, north=45.53, east=12.28,
    )
    db_session.add(across_the_edge)
    db_session.commit()

    run(across_the_edge, sync_session_factory, fake_overpass)

    assert reload(db_session, across_the_edge).sync_state == SyncState.READY
    assert len(fake_overpass.boxes) == 1


def test_a_final_failure_marks_the_destination_failed(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    db_session.add(Place(destination_id=venice.id, name="Mine", category="cafe"))
    db_session.commit()
    fake_overpass.error = OverpassError("OpenStreetMap's place service is busy right now.")

    run(venice, sync_session_factory, fake_overpass)

    destination = reload(db_session, venice)
    assert destination.sync_state == SyncState.FAILED
    assert destination.sync_error == "OpenStreetMap's place service is busy right now."
    # saved places are untouched.
    assert db_session.scalar(select(func.count()).select_from(Place)) == 1


def test_an_unexpected_error_marks_the_destination_failed(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    """regression test: a non-overpass error used to leave it stuck on syncing."""
    fake_overpass.error = RuntimeError("database went away")

    run(venice, sync_session_factory, fake_overpass)

    destination = reload(db_session, venice)
    assert destination.sync_state == SyncState.FAILED
    assert destination.sync_error == UNEXPECTED_ERROR
    # and the claim is released, so retry can run.
    assert venice.id not in sync_module._active


def test_an_oversized_older_destination_fails_without_a_query(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    # as a destination created under the old 2-degree clamp could be.
    venice.north = venice.south + MAX_SPAN_DEGREES + 0.5
    db_session.commit()

    run(venice, sync_session_factory, fake_overpass)

    destination = reload(db_session, venice)
    assert destination.sync_state == SyncState.FAILED
    assert "too large" in (destination.sync_error or "")
    assert fake_overpass.boxes == []


def test_re_syncing_updates_places_in_place(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    fake_overpass.elements = elements_in_venice()
    run(venice, sync_session_factory, fake_overpass)

    renamed = elements_in_venice()
    renamed[0]["tags"]["name"] = "Da Mario Nuovo"
    fake_overpass.elements = renamed
    run(venice, sync_session_factory, fake_overpass)

    db_session.expire_all()
    assert db_session.scalar(select(func.count()).select_from(OsmPlace)) == 2
    assert db_session.scalar(
        select(OsmPlace.name).where(OsmPlace.source_ref == "node/1")
    ) == "Da Mario Nuovo"


def test_a_destination_deleted_during_the_fetch(
    db_session: Session,
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
) -> None:
    """regression test: writing to the stale copy used to fail the commit."""
    fake_overpass.elements = elements_in_venice()
    venice_id = venice.id

    def delete_it() -> None:
        # another session (the delete request) removes it mid-fetch.
        with sync_session_factory() as other:
            other.delete(other.get(Destination, venice_id))
            other.commit()

    fake_overpass.on_fetch = delete_it

    run(venice, sync_session_factory, fake_overpass)

    db_session.expire_all()
    assert db_session.get(Destination, venice_id) is None
    # the shared cache still got the places.
    assert db_session.scalar(select(func.count()).select_from(OsmPlace)) == 2


def test_a_sync_already_running_is_not_started_twice(
    venice: Destination,
    sync_session_factory: sessionmaker[Session],
    fake_overpass: FakeOverpass,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync_module, "_active", {venice.id})

    run(venice, sync_session_factory, fake_overpass)

    assert fake_overpass.boxes == []


# --- area clamping -----------------------------------------------------------


def test_clamp_area_leaves_new_york_city_alone() -> None:
    """nyc's real box (0.44 x 0.56) fits in one query."""
    box = (40.4774, -74.2591, 40.9176, -73.7004)
    assert clamp_area(40.7128, -74.0060, *box) == box


def test_clamp_area_cuts_a_huge_box_to_the_query_limit() -> None:
    south, west, north, east = clamp_area(35.68, 139.69, 20.0, 120.0, 50.0, 160.0)
    assert north - south <= MAX_DESTINATION_SPAN_DEGREES + 1e-9
    assert east - west <= MAX_DESTINATION_SPAN_DEGREES + 1e-9
    assert MAX_DESTINATION_SPAN_DEGREES == MAX_SPAN_DEGREES
