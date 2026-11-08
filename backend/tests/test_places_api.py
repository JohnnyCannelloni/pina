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
