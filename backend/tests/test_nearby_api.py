"""GET /api/destinations/{id}/nearby: places or clusters for a viewport."""

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Destination, ExtractState, OsmExtract, OsmPlace
from app.routes import CLUSTER_THRESHOLD

# a box around central venice.
BBOX = {"south": 45.430, "west": 12.325, "north": 45.445, "east": 12.345}


def cache_place(
    db: Session,
    source_ref: str,
    name: str,
    category: str,
    latitude: float,
    longitude: float,
    region: str = "europe/italy",
) -> None:
    """put one place in the global osm cache, as an import would."""
    db.add(
        OsmPlace(
            source_ref=source_ref,
            name=name,
            category=category,
            latitude=latitude,
            longitude=longitude,
            region=region,
        )
    )
    db.commit()


def cache_many(
    db: Session,
    count: int,
    south: float,
    west: float,
    north: float,
    east: float,
) -> None:
    """spread `count` places evenly over a box, in one insert."""
    rows = []
    side = int(count**0.5) + 1
    for index in range(count):
        row, column = divmod(index, side)
        rows.append(
            {
                "source_ref": f"node/bulk{index}",
                "name": f"Place {index}",
                "category": "restaurant",
                "latitude": south + (north - south) * (row / side),
                "longitude": west + (east - west) * (column / side),
                "region": "europe/italy",
                "fetched_at": datetime.now(UTC),
            }
        )
    db.execute(OsmPlace.__table__.insert(), rows)
    db.commit()


def ready_extract(
    db: Session,
    region: str,
    south: float,
    west: float,
    north: float,
    east: float,
) -> None:
    """record a region as imported, covering the given box."""
    db.add(
        OsmExtract(
            region=region,
            state=ExtractState.READY,
            south=south,
            west=west,
            north=north,
            east=east,
        )
    )
    db.commit()


# --- places mode -------------------------------------------------------------


def test_nearby_returns_places_below_the_threshold(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """a sparse viewport returns pins."""
    cache_place(db_session, "node/1", "Trattoria Rosa", "restaurant", 45.4371, 12.3326)
    cache_place(db_session, "node/2", "Caffè Centrale", "cafe", 45.4372, 12.3327)

    response = client.get(f"/api/destinations/{venice.id}/nearby", params=BBOX)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["mode"] == "places"
    assert body["total"] == 2
    assert {item["name"] for item in body["places"]} == {
        "Trattoria Rosa",
        "Caffè Centrale",
    }
    assert body["clusters"] == []


def test_nearby_filters_by_bounding_box(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """places outside the rectangle aren't returned (but stay cached)."""
    cache_place(db_session, "node/1", "Inside", "restaurant", 45.4371, 12.3326)
    cache_place(db_session, "node/2", "Far away", "restaurant", 41.9028, 12.4964)

    body = client.get(
        f"/api/destinations/{venice.id}/nearby", params=BBOX
    ).json()
    assert [item["name"] for item in body["places"]] == ["Inside"]
    assert body["total"] == 1


def test_nearby_filters_by_category(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    cache_place(db_session, "node/1", "A Restaurant", "restaurant", 45.4371, 12.3326)
    cache_place(db_session, "node/2", "A Café", "cafe", 45.4372, 12.3327)
    cache_place(db_session, "node/3", "A Sight", "sight", 45.4373, 12.3328)

    body = client.get(
        f"/api/destinations/{venice.id}/nearby",
        params={**BBOX, "categories": ["cafe", "sight"]},
    ).json()
    assert {item["name"] for item in body["places"]} == {"A Café", "A Sight"}
    assert body["total"] == 2


# --- the global store --------------------------------------------------------


def test_two_destinations_share_one_set_of_rows(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """one cached row serves every destination that overlaps it."""
    mestre = Destination(
        trip_id=venice.trip_id,
        name="Mestre",
        latitude=45.4371,
        longitude=12.3326,
        south=45.430,
        west=12.325,
        north=45.445,
        east=12.345,
    )
    db_session.add(mestre)
    db_session.commit()

    cache_place(db_session, "node/1", "Shared Café", "cafe", 45.4371, 12.3326)

    for destination_id in (venice.id, mestre.id):
        body = client.get(
            f"/api/destinations/{destination_id}/nearby", params=BBOX
        ).json()
        assert [item["name"] for item in body["places"]] == ["Shared Café"]

    # one row backing both.
    assert db_session.query(OsmPlace).count() == 1


def test_already_saved_is_per_destination(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """places are shared, but "already saved" is per destination."""
    mestre = Destination(
        trip_id=venice.trip_id,
        name="Mestre",
        latitude=45.4371,
        longitude=12.3326,
        south=45.430,
        west=12.325,
        north=45.445,
        east=12.345,
    )
    db_session.add(mestre)
    db_session.commit()

    cache_place(db_session, "node/1", "Shared Café", "cafe", 45.4371, 12.3326)

    saved = client.post(
        f"/api/destinations/{venice.id}/places",
        json={
            "name": "Shared Café",
            "category": "cafe",
            "latitude": 45.4371,
            "longitude": 12.3326,
            "source": "osm",
            "source_ref": "node/1",
        },
    )
    assert saved.status_code == 201, saved.text

    in_venice = client.get(
        f"/api/destinations/{venice.id}/nearby", params=BBOX
    ).json()["places"]
    in_mestre = client.get(
        f"/api/destinations/{mestre.id}/nearby", params=BBOX
    ).json()["places"]

    assert in_venice[0]["already_saved"] is True
    assert in_mestre[0]["already_saved"] is False


# --- clusters mode -----------------------------------------------------------


def test_nearby_clusters_above_the_threshold(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """a dense viewport returns counts that add up to the total."""
    count = CLUSTER_THRESHOLD + 50
    cache_many(db_session, count, 45.430, 12.325, 45.445, 12.345)

    body = client.get(f"/api/destinations/{venice.id}/nearby", params=BBOX).json()

    assert body["mode"] == "clusters"
    assert body["total"] == count
    assert body["places"] == []
    assert len(body["clusters"]) > 1, "a spread-out grid should make many cells"
    assert sum(cluster["count"] for cluster in body["clusters"]) == count


def test_cluster_positions_sit_inside_the_viewport(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """bubbles sit at the average position of their places."""
    cache_many(db_session, CLUSTER_THRESHOLD + 50, 45.430, 12.325, 45.445, 12.345)

    body = client.get(f"/api/destinations/{venice.id}/nearby", params=BBOX).json()
    # small tolerance: these are float averages.
    epsilon = 1e-9
    for cluster in body["clusters"]:
        assert BBOX["south"] - epsilon <= cluster["latitude"] <= BBOX["north"] + epsilon
        assert BBOX["west"] - epsilon <= cluster["longitude"] <= BBOX["east"] + epsilon


# --- coverage ----------------------------------------------------------------


def test_coverage_missing_when_nothing_is_imported(
    client: TestClient, venice: Destination
) -> None:
    """an empty map says why it's empty."""
    body = client.get(f"/api/destinations/{venice.id}/nearby", params=BBOX).json()
    assert body["coverage"] == "missing"
    assert body["total"] == 0


def test_coverage_covered_when_an_extract_contains_the_viewport(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    ready_extract(db_session, "europe/italy", 35.0, 6.0, 47.0, 19.0)

    body = client.get(f"/api/destinations/{venice.id}/nearby", params=BBOX).json()
    assert body["coverage"] == "covered"


def test_coverage_partial_when_the_viewport_overhangs(
    client: TestClient, venice: Destination, db_session: Session
) -> None:
    """a viewport half inside an imported region is partial."""
    # covers only the southern half of the box.
    ready_extract(db_session, "europe/italy", 45.300, 12.000, 45.4375, 13.000)

    body = client.get(f"/api/destinations/{venice.id}/nearby", params=BBOX).json()
    assert body["coverage"] == "partial"


def test_nearby_404s_for_an_unknown_destination(client: TestClient) -> None:
    response = client.get("/api/destinations/9999/nearby", params=BBOX)
    assert response.status_code == 404, response.text
