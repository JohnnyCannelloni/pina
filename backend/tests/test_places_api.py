"""saving, reading and deleting places."""

from fastapi.testclient import TestClient

from app.models import Destination

# a real spot in venice. compared exactly after the round trip.
PIN_LATITUDE = 45.43713
PIN_LONGITUDE = 12.33265


def test_save_a_place_with_a_pin_and_read_it_back(
    client: TestClient, venice: Destination
) -> None:
    """save a place, then read it back from the database."""
    # save
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

    # the response carries the generated fields.
    assert created["name"] == "Trattoria Rosa"
    assert created["category"] == "restaurant"
    assert created["must_go"] is True
    assert created["destination_id"] == venice.id
    assert isinstance(created["id"], int)
    # set by the database default.
    assert created["created_at"] is not None

    # read it back in a separate request.
    response = client.get(f"/api/destinations/{venice.id}/places")
    assert response.status_code == 200, response.text

    places = response.json()
    assert len(places) == 1

    place = places[0]
    assert place["id"] == created["id"]
    assert place["name"] == "Trattoria Rosa"
    assert place["category"] == "restaurant"
    assert place["must_go"] is True

    # exact equality: double precision round-trips through json.
    assert place["latitude"] == PIN_LATITUDE
    assert place["longitude"] == PIN_LONGITUDE


def test_places_without_a_pin(client: TestClient, venice: Destination) -> None:
    """no pin is valid; half a pin is rejected."""
    # no coordinates: accepted and still listed.
    response = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Caffè Florian", "category": "cafe", "must_go": False},
    )
    assert response.status_code == 201, response.text
    assert response.json()["latitude"] is None
    assert response.json()["longitude"] is None

    listed = client.get(f"/api/destinations/{venice.id}/places").json()
    assert [p["name"] for p in listed] == ["Caffè Florian"]

    # half a pin: 422 from PlaceCreate.
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

    # and nothing was saved.
    listed = client.get(f"/api/destinations/{venice.id}/places").json()
    assert [p["name"] for p in listed] == ["Caffè Florian"]


def test_a_manual_place_defaults_to_source_manual(
    client: TestClient, venice: Destination
) -> None:
    """a body without source still works, as 'manual'."""
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
    """saving a map suggestion keeps its osm reference."""
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
    """saving the same osm place twice is a 409."""
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

    # still one row, and the session recovered from the failed commit.
    places = client.get(f"/api/destinations/{venice.id}/places").json()
    assert len(places) == 1


def test_source_and_source_ref_must_agree(
    client: TestClient, venice: Destination
) -> None:
    """osm without a ref and manual with one are both rejected."""
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
    """manual places don't collide on the unique index (nulls are distinct)."""
    for name in ("First", "Second", "Third"):
        response = client.post(
            f"/api/destinations/{venice.id}/places",
            json={"name": name, "category": "cafe"},
        )
        assert response.status_code == 201, response.text

    assert len(client.get(f"/api/destinations/{venice.id}/places").json()) == 3


# --- deleting ----------------------------------------------------------------


def test_delete_a_place(client: TestClient, venice: Destination) -> None:
    """delete removes the row."""
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
    """a place can only be deleted through its own destination."""
    created = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "Caffè Florian", "category": "cafe"},
    ).json()

    response = client.delete(f"/api/destinations/999999/places/{created['id']}")
    assert response.status_code == 404, response.text

    # still there.
    assert len(client.get(f"/api/destinations/{venice.id}/places").json()) == 1


def test_a_deleted_osm_place_can_be_saved_again(
    client: TestClient, venice: Destination
) -> None:
    """deleting frees the slot, so re-saving isn't a 409."""
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
