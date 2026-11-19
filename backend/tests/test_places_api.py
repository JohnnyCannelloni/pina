"""Integration tests for saving and reading back places.

See conftest.py for why these run against a real Postgres migrated by the real
Alembic migration.
"""

from fastapi.testclient import TestClient

from app.models import Destination

# A real spot in Venice. The exact digits matter to the first test: they are
# asserted for equality after a full round trip through JSON and Postgres.
PIN_LATITUDE = 45.43713
PIN_LONGITUDE = 12.33265


def test_save_a_place_with_a_pin_and_read_it_back(
    client: TestClient, venice: Destination
) -> None:
    """The main one: save a place, then read it back out of the database.

    This is the whole feature in one test. If it passes, the chain from HTTP
    body -> Pydantic -> SQLAlchemy -> INSERT -> SELECT -> JSON works.
    """
    # --- save ---------------------------------------------------------------
    response = client.post(
        f"/api/destinations/{venice.id}/places",
        json={
            "name": "Trattoria Rosa",
            "category": "restaurant",
            "must_go": True,
            "latitude": PIN_LATITUDE,
            "longitude": PIN_LONGITUDE,
        },
    )

    assert response.status_code == 201, response.text
    created = response.json()

    # The response carries the saved row, including what the database generated.
    assert created["name"] == "Trattoria Rosa"
    assert created["category"] == "restaurant"
    assert created["must_go"] is True
    assert created["destination_id"] == venice.id
    assert isinstance(created["id"], int)
    # Proof that server_default now() fired, rather than the app sending a time.
    assert created["created_at"] is not None

    # --- read it back -------------------------------------------------------
    # A separate request, so the assertions below are about what is in Postgres,
    # not about what the POST happened to return.
    response = client.get(f"/api/destinations/{venice.id}/places")
    assert response.status_code == 200, response.text

    places = response.json()
    assert len(places) == 1

    place = places[0]
    assert place["id"] == created["id"]
    assert place["name"] == "Trattoria Rosa"
    assert place["category"] == "restaurant"
    assert place["must_go"] is True

    # Exact equality, not pytest.approx. DOUBLE PRECISION is IEEE-754 float64,
    # the same type as a Python float and a JavaScript number, so the pin makes
    # the trip browser -> JSON -> Postgres -> JSON -> browser without losing a
    # bit. This assertion is what "find its pin in the same spot" means at the
    # data layer — and it would fail if the column were NUMERIC(9,6) or REAL.
    assert place["latitude"] == PIN_LATITUDE
    assert place["longitude"] == PIN_LONGITUDE


def test_places_without_a_pin(client: TestClient, venice: Destination) -> None:
    """A place saved with no pin is still a place.

    Covers the two rules the map introduced:
      - no coordinates is valid, stored as NULL, and still listed
      - one coordinate without the other is rejected before it reaches the table
    """
    # No coordinates at all: accepted, stored as NULL, still in the list.
    response = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Caffè Florian", "category": "cafe", "must_go": False},
    )
    assert response.status_code == 201, response.text
    assert response.json()["latitude"] is None
    assert response.json()["longitude"] is None

    listed = client.get(f"/api/destinations/{venice.id}/places").json()
    assert [p["name"] for p in listed] == ["Caffè Florian"]

    # Half a pin: rejected with a 422 by PlaceCreate's model_validator, so it
    # never reaches ck_places_coords_both_or_neither. Both layers enforce this;
    # this asserts the one that produces a readable error.
    response = client.post(
        f"/api/destinations/{venice.id}/places",
        json={
            "name": "Half a pin",
            "category": "sight",
            "latitude": PIN_LATITUDE,
        },
    )
    assert response.status_code == 422, response.text
    assert "together" in response.text

    # And the rejected place was not saved.
    listed = client.get(f"/api/destinations/{venice.id}/places").json()
    assert [p["name"] for p in listed] == ["Caffè Florian"]


def test_a_manual_place_defaults_to_source_manual(
    client: TestClient, venice: Destination
) -> None:
    """A body that says nothing about source still works, as 'manual'.

    This is the compatibility check for migration 0002: the exact POST body the
    save form sent before source existed must keep working.
    """
    response = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Osteria Anice Stellato", "category": "restaurant"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["source"] == "manual"
    assert response.json()["source_ref"] is None


def test_save_an_osm_suggestion_and_read_it_back(
    client: TestClient, venice: Destination
) -> None:
    """Saving a map suggestion keeps the OSM reference that identifies it."""
    response = client.post(
        f"/api/destinations/{venice.id}/places",
        json={
            "name": "Trattoria alla Madonna",
            "category": "restaurant",
            "must_go": True,
            "latitude": PIN_LATITUDE,
            "longitude": PIN_LONGITUDE,
            "source": "osm",
            "source_ref": "node/1234567",
        },
    )
    assert response.status_code == 201, response.text

    places = client.get(f"/api/destinations/{venice.id}/places").json()
    assert len(places) == 1

    place = places[0]
    assert place["source"] == "osm"
    assert place["source_ref"] == "node/1234567"
    assert place["must_go"] is True
    assert place["latitude"] == PIN_LATITUDE
    assert place["longitude"] == PIN_LONGITUDE


def test_saving_the_same_osm_place_twice_is_a_conflict(
    client: TestClient, venice: Destination
) -> None:
    """The second save of one OSM place is a 409, enforced by the database.

    ix_places_destination_source_ref (migration 0002) is what refuses it;
    create_place turns the resulting IntegrityError into a 409 Conflict. 409
    rather than 400 because the request is well-formed — it just conflicts with
    what is already there.
    """
    body = {
        "name": "Trattoria alla Madonna",
        "category": "restaurant",
        "latitude": PIN_LATITUDE,
        "longitude": PIN_LONGITUDE,
        "source": "osm",
        "source_ref": "node/1234567",
    }

    assert client.post(f"/api/destinations/{venice.id}/places", json=body).status_code == 201

    second = client.post(f"/api/destinations/{venice.id}/places", json=body)
    assert second.status_code == 409, second.text
    assert "already saved" in second.json()["detail"]

    # One row, not two — and the session recovered from the failed commit
    # (create_place rolls back), so this read still works.
    places = client.get(f"/api/destinations/{venice.id}/places").json()
    assert len(places) == 1


def test_source_and_source_ref_must_agree(
    client: TestClient, venice: Destination
) -> None:
    """osm without a ref, and manual with one, are both rejected.

    Mirrors ck_places_source_ref_present. Rejected at the schema layer with a
    422 so the message names the problem; the constraint stands behind it.
    """
    missing_ref = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "No ref", "category": "cafe", "source": "osm"},
    )
    assert missing_ref.status_code == 422, missing_ref.text
    assert "source_ref" in missing_ref.text

    unexpected_ref = client.post(
        f"/api/destinations/{venice.id}/places",
        json={
            "name": "Hand typed",
            "category": "cafe",
            "source": "manual",
            "source_ref": "node/999",
        },
    )
    assert unexpected_ref.status_code == 422, unexpected_ref.text

    assert client.get(f"/api/destinations/{venice.id}/places").json() == []


def test_two_manual_places_do_not_collide_on_null_source_ref(
    client: TestClient, venice: Destination
) -> None:
    """Many manual places coexist despite the unique index.

    This is the Postgres NULL rule from migration 0002 made concrete: a unique
    index treats NULLs as distinct, so every manual place has source_ref NULL
    and none of them conflict. Worth a test precisely because most people
    expect the opposite and add a partial index they do not need.
    """
    for name in ("First", "Second", "Third"):
        response = client.post(
            f"/api/destinations/{venice.id}/places",
            json={"name": name, "category": "cafe"},
        )
        assert response.status_code == 201, response.text

    assert len(client.get(f"/api/destinations/{venice.id}/places").json()) == 3


# --- deleting ---------------------------------------------------------------


def test_delete_a_place(client: TestClient, venice: Destination) -> None:
    """Delete removes the row; a fresh read no longer returns it."""
    created = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Caffè Florian", "category": "cafe"},
    ).json()

    response = client.delete(f"/api/destinations/{venice.id}/places/{created['id']}")
    assert response.status_code == 204, response.text
    assert response.content == b""

    assert client.get(f"/api/destinations/{venice.id}/places").json() == []


def test_delete_an_unknown_place_is_a_404(
    client: TestClient, venice: Destination
) -> None:
    response = client.delete(f"/api/destinations/{venice.id}/places/999999")
    assert response.status_code == 404, response.text


def test_delete_through_the_wrong_destination_is_a_404(
    client: TestClient, venice: Destination
) -> None:
    """A place id is only deletable through the destination it belongs to."""
    created = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Caffè Florian", "category": "cafe"},
    ).json()

    response = client.delete(f"/api/destinations/999999/places/{created['id']}")
    assert response.status_code == 404, response.text

    # Still there.
    assert len(client.get(f"/api/destinations/{venice.id}/places").json()) == 1


def test_a_deleted_osm_place_can_be_saved_again(
    client: TestClient, venice: Destination
) -> None:
    """Deleting frees the (destination, source_ref) slot, so no 409 on re-save."""
    body = {
        "name": "Trattoria alla Madonna",
        "category": "restaurant",
        "latitude": PIN_LATITUDE,
        "longitude": PIN_LONGITUDE,
        "source": "osm",
        "source_ref": "node/1234567",
    }
    created = client.post(f"/api/destinations/{venice.id}/places", json=body).json()
    client.delete(f"/api/destinations/{venice.id}/places/{created['id']}")

    response = client.post(f"/api/destinations/{venice.id}/places", json=body)
    assert response.status_code == 201, response.text
