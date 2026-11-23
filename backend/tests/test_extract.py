"""region resolution and the extract importer.

no network: regions are tested against a small literal index, and imports
against a real .osm.pbf written by osmium during the test.
"""

import json
import threading
from pathlib import Path

import osmium
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app import extract as extract_module
from app import regions as regions_module
from app.extract import (
    ExtractCancelled,
    ExtractError,
    download_extract,
    import_extract,
)
from app.models import (
    Destination,
    ExtractState,
    OsmExtract,
    OsmPlace,
    Place,
    SyncState,
)
from app.regions import (
    Region,
    UnsupportedRegion,
    candidates_for,
    find_region,
    resolve_region,
)
from app.sync import MAX_DESTINATION_SPAN_DEGREES, clamp_area, sync_destination

REGION = "europe/testland"


def write_pbf(path: Path, nodes: list[tuple[int, float, float, dict[str, str]]]) -> None:
    """write a real .osm.pbf with the given (id, latitude, longitude, tags) nodes.

    osmium's location is (lon, lat).
    """
    # SimpleWriter won't overwrite, and some tests write the same path twice.
    path.unlink(missing_ok=True)
    with osmium.SimpleWriter(str(path)) as writer:
        for node_id, latitude, longitude, tags in nodes:
            writer.add_node(
                osmium.osm.mutable.Node(
                    id=node_id, location=(longitude, latitude), tags=tags
                )
            )


def square(west: float, south: float, east: float, north: float) -> list:
    """a closed geojson ring, in [longitude, latitude] order."""
    return [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]


@pytest.fixture
def cached_extract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """point the extract cache at a temp dir and return the fixture's path."""
    monkeypatch.setattr(
        extract_module.settings, "EXTRACT_CACHE_DIR", str(tmp_path)
    )
    return extract_module.extract_path(REGION)


@pytest.fixture
def fake_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """a tiny geofabrik-style index: a country with a nested sub-region."""
    index = {
        "features": [
            {
                "properties": {
                    "id": "bigland",
                    "name": "Bigland",
                    "urls": {"pbf": "https://example.test/bigland.osm.pbf"},
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [square(0.0, 0.0, 10.0, 10.0)],
                },
            },
            {
                "properties": {
                    "id": "bigland/smallstate",
                    "name": "Bigland / Smallstate",
                    "urls": {"pbf": "https://example.test/smallstate.osm.pbf"},
                },
                "geometry": {
                    "type": "MultiPolygon",
                    "coordinates": [[square(1.0, 1.0, 3.0, 3.0)]],
                },
            },
            {
                # l-shaped: its bounding box covers (2.5, 2.5) but its shape
                # doesn't.
                "properties": {
                    "id": "bigland/lshape",
                    "name": "Bigland / L",
                    "urls": {"pbf": "https://example.test/lshape.osm.pbf"},
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [2.0, 2.0],
                            [4.0, 2.0],
                            [4.0, 2.4],
                            [2.4, 2.4],
                            [2.4, 4.0],
                            [2.0, 4.0],
                            [2.0, 2.0],
                        ]
                    ],
                },
            },
        ]
    }
    path = tmp_path / "geofabrik-index.json"
    path.write_text(json.dumps(index))
    monkeypatch.setattr(
        regions_module.settings, "EXTRACT_CACHE_DIR", str(tmp_path)
    )
    # clear the in-process index cache between tests.
    monkeypatch.setattr(regions_module, "_regions", None)


# --- region resolution -------------------------------------------------------


def test_resolve_picks_the_smallest_containing_region(fake_index: None) -> None:
    """a point in both a country and its sub-region resolves to the sub-region."""
    # well inside smallstate and outside the l-shape. not on a vertex, where
    # ray casting is undefined.
    chosen = resolve_region(1.5, 1.5)
    assert chosen.id == "bigland/smallstate"

    # the country really is a candidate too.
    assert [region.id for region in candidates_for(1.5, 1.5)] == [
        "bigland/smallstate",
        "bigland",
    ]


def test_resolve_falls_back_to_the_country(fake_index: None) -> None:
    """somewhere without a sub-region still resolves to the country."""
    assert resolve_region(8.0, 8.0).id == "bigland"


def test_resolve_uses_real_geometry_not_the_bounding_box(
    fake_index: None,
) -> None:
    """a point inside a region's box but outside its shape is excluded.

    regression test: a bounding-box check put new york city in new jersey and
    lisbon in spain.
    """
    lshape = find_region("bigland/lshape")
    assert lshape.in_bounds(2.5, 2.5)  # the bbox does contain the point
    assert not lshape.contains(2.5, 2.5)  # the shape does not

    assert resolve_region(2.5, 2.5).id == "bigland/smallstate"


def test_resolve_raises_when_nothing_contains_the_point(
    fake_index: None,
) -> None:
    """mid-ocean raises instead of returning None."""
    with pytest.raises(UnsupportedRegion):
        resolve_region(-40.0, -140.0)


def test_region_url_comes_from_the_index(fake_index: None) -> None:
    """the download url comes from the index, not from the id."""
    assert (
        find_region("bigland/smallstate").pbf_url
        == "https://example.test/smallstate.osm.pbf"
    )


def test_multipolygon_and_polygon_are_both_handled(fake_index: None) -> None:
    """polygon and multipolygon geometries are both handled."""
    assert find_region("bigland").contains(5.0, 5.0)  # Polygon
    assert find_region("bigland/smallstate").contains(1.5, 1.5)  # MultiPolygon


# --- area clamping -----------------------------------------------------------


def test_clamp_area_leaves_new_york_city_alone() -> None:
    """new york city's real box isn't clamped (regression for the old 0.12 limit)."""
    south, west, north, east = clamp_area(
        40.7128, -74.0060, 40.4774, -74.2591, 40.9176, -73.7004
    )
    assert north - south == pytest.approx(0.4402, abs=1e-3)
    assert east - west == pytest.approx(0.5587, abs=1e-3)


def test_clamp_area_still_rejects_a_continent() -> None:
    """the loose sanity limit still applies."""
    south, west, north, east = clamp_area(0.0, 0.0, -45.0, -90.0, 45.0, 90.0)
    assert north - south <= MAX_DESTINATION_SPAN_DEGREES + 1e-9
    assert east - west <= MAX_DESTINATION_SPAN_DEGREES + 1e-9


# --- importing ---------------------------------------------------------------


def test_import_maps_tags_and_drops_junk(
    db_session: Session, cached_extract: Path
) -> None:
    """tags map to the right category; unusable nodes are skipped."""
    write_pbf(
        cached_extract,
        [
            (1, 45.4371, 12.3326, {"name": "Trattoria Rosa", "amenity": "restaurant"}),
            (2, 45.4372, 12.3327, {"name": "Pizza Window", "amenity": "fast_food"}),
            (3, 45.4373, 12.3328, {"name": "Caffè Centrale", "amenity": "cafe"}),
            (4, 45.4374, 12.3329, {"name": "Gelateria", "amenity": "ice_cream"}),
            (5, 45.4375, 12.3330, {"name": "Palazzo Ducale", "tourism": "attraction"}),
            (6, 45.4376, 12.3331, {"name": "Galleria", "tourism": "gallery"}),
            (7, 45.4377, 12.3332, {"amenity": "restaurant"}),  # no name
            (8, 45.4378, 12.3333, {"name": "A Bench", "amenity": "bench"}),  # unmapped
        ],
    )

    assert import_extract(db_session, REGION) == 6

    rows = list(db_session.scalars(select(OsmPlace)))
    assert {row.name: row.category for row in rows} == {
        "Trattoria Rosa": "restaurant",
        "Pizza Window": "restaurant",
        "Caffè Centrale": "cafe",
        "Gelateria": "cafe",
        "Palazzo Ducale": "sight",
        "Galleria": "sight",
    }
    # provenance.
    assert {row.region for row in rows} == {REGION}


def test_import_keeps_the_useful_tags(
    db_session: Session, cached_extract: Path
) -> None:
    """whitelisted tags land in osm_places.tags, cleaned.

    covers values seen in real data: a cuisine list, a bare "www." site,
    contact:* keys, two phone numbers, and a javascript: "website".
    """
    write_pbf(
        cached_extract,
        [
            (
                1,
                45.4371,
                12.3326,
                {
                    "name": "Trattoria Rosa",
                    "amenity": "restaurant",
                    "cuisine": "Italian; pizza",
                    "outdoor_seating": "yes",
                    "diet:vegetarian": "only",
                    "opening_hours": "Mo-Su 12:00-23:00",
                    "contact:website": "www.trattoriarosa.it",
                    "contact:phone": "+39 041 123;+39 041 456",
                    "toilets": "yes",  # not whitelisted
                },
            ),
            (
                2,
                45.4372,
                12.3327,
                {
                    "name": "Sneaky Bar",
                    "amenity": "cafe",
                    "website": "javascript:alert(1)",
                },
            ),
            (3, 45.4373, 12.3328, {"name": "Plain Café", "amenity": "cafe"}),
        ],
    )

    import_extract(db_session, REGION)

    tags = {
        row.name: row.tags for row in db_session.scalars(select(OsmPlace))
    }
    assert tags["Trattoria Rosa"] == {
        "cuisine": ["italian", "pizza"],
        "outdoor_seating": "yes",
        "diet:vegetarian": "only",
        "opening_hours": "Mo-Su 12:00-23:00",
        "website": "https://www.trattoriarosa.it",
        "phone": "+39 041 123",
    }
    # the bad link is dropped, leaving no tags at all.
    assert tags["Sneaky Bar"] is None
    assert tags["Plain Café"] is None

    # sql null, not json null.
    assert db_session.scalar(
        select(func.count()).select_from(OsmPlace).where(OsmPlace.tags.is_(None))
    ) == 2

    # the containment query app/ask.py relies on.
    italian = db_session.scalars(
        select(OsmPlace.name).where(OsmPlace.tags.contains({"cuisine": ["pizza"]}))
    ).all()
    assert italian == ["Trattoria Rosa"]


def test_reimport_updates_tags(db_session: Session, cached_extract: Path) -> None:
    """a place imported before 0006 gets tags on the next import."""
    write_pbf(
        cached_extract,
        [(1, 45.4371, 12.3326, {"name": "Rosa", "amenity": "restaurant"})],
    )
    import_extract(db_session, REGION)

    write_pbf(
        cached_extract,
        [
            (
                1,
                45.4371,
                12.3326,
                {"name": "Rosa", "amenity": "restaurant", "cuisine": "italian"},
            )
        ],
    )
    import_extract(db_session, REGION)

    row = db_session.scalar(select(OsmPlace))
    assert row is not None
    db_session.refresh(row)
    assert row.tags == {"cuisine": ["italian"]}


def test_import_keeps_the_whole_region(
    db_session: Session, venice: Destination, cached_extract: Path
) -> None:
    """places far from any destination are still cached."""
    write_pbf(
        cached_extract,
        [
            (1, 45.4371, 12.3326, {"name": "In Venice", "amenity": "restaurant"}),
            (2, 41.9028, 12.4964, {"name": "In Rome", "amenity": "restaurant"}),
        ],
    )

    assert import_extract(db_session, REGION) == 2
    names = set(db_session.scalars(select(OsmPlace.name)))
    assert names == {"In Venice", "In Rome"}


def test_import_is_idempotent_and_updates_renamed_places(
    db_session: Session, cached_extract: Path
) -> None:
    """re-importing updates rows instead of duplicating them."""
    write_pbf(
        cached_extract,
        [(1, 45.4371, 12.3326, {"name": "Old Name", "amenity": "restaurant"})],
    )
    import_extract(db_session, REGION)

    write_pbf(
        cached_extract,
        [(1, 45.4371, 12.3326, {"name": "New Name", "amenity": "restaurant"})],
    )
    import_extract(db_session, REGION)

    rows = list(db_session.scalars(select(OsmPlace)))
    assert len(rows) == 1
    assert rows[0].name == "New Name"


def test_import_without_a_downloaded_file_raises(
    db_session: Session, cached_extract: Path
) -> None:
    """a missing extract is an error, not an empty success."""
    with pytest.raises(ExtractError, match="not been downloaded"):
        import_extract(db_session, REGION)


# --- the sync ----------------------------------------------------------------


def test_sync_imports_and_records_coverage(
    db_session: Session,
    sync_session_factory: sessionmaker[Session],
    venice: Destination,
    cached_extract: Path,
    fake_index: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a full run fills the cache, marks ready and records coverage bounds."""
    # point the destination at the fixture region the fake index doesn't know.
    venice.region = REGION
    db_session.commit()
    monkeypatch.setattr(
        extract_module,
        "find_region",
        lambda _: Region(
            id=REGION,
            name=REGION,
            south=45.0,
            west=12.0,
            north=46.0,
            east=13.0,
            pbf_url="https://example.test/testland.osm.pbf",
        ),
    )
    import app.sync as sync_module

    monkeypatch.setattr(sync_module, "find_region", extract_module.find_region)

    write_pbf(
        cached_extract,
        [
            (1, 45.4371, 12.3326, {"name": "Trattoria Rosa", "amenity": "restaurant"}),
            (2, 45.4372, 12.3327, {"name": "Caffè Centrale", "amenity": "cafe"}),
        ],
    )

    sync_destination(venice.id, session_factory=sync_session_factory)

    db_session.expire_all()
    refreshed = db_session.get(Destination, venice.id)
    assert refreshed is not None
    assert refreshed.sync_state == SyncState.READY

    assert db_session.scalar(select(func.count()).select_from(OsmPlace)) == 2

    from app.models import OsmExtract

    extract = db_session.scalar(
        select(OsmExtract).where(OsmExtract.region == REGION)
    )
    assert extract is not None
    assert extract.state == ExtractState.READY
    assert extract.place_count == 2
    # coverage recorded.
    assert (extract.south, extract.north) == (45.0, 46.0)


def test_sync_reports_an_unsupported_location_without_touching_saved_places(
    db_session: Session,
    sync_session_factory: sessionmaker[Session],
    venice: Destination,
    fake_index: None,
    client,
) -> None:
    """an unsupported location fails clearly and leaves saved places alone."""
    saved = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "My Own Place", "category": "cafe"},
    )
    assert saved.status_code == 201, saved.text

    venice.region = None
    # mid-pacific: the fixture index only covers 0..10 degrees.
    venice.latitude, venice.longitude = -40.0, -140.0
    db_session.commit()

    sync_destination(venice.id, session_factory=sync_session_factory)

    db_session.expire_all()
    refreshed = db_session.get(Destination, venice.id)
    assert refreshed is not None
    assert refreshed.sync_state == SyncState.FAILED
    assert refreshed.sync_error is not None

    assert db_session.scalar(select(func.count()).select_from(OsmPlace)) == 0
    assert db_session.scalar(
        select(func.count())
        .select_from(Place)
        .where(Place.destination_id == venice.id)
    ) == 1


# --- cancelling a download ---------------------------------------------------


class _FakeStream:
    """fake httpx.stream: a 200 response of `chunks` 1-byte chunks.

    `on_chunk` runs per chunk, so a test can cancel partway through.
    """

    status_code = 200
    is_error = False

    def __init__(self, chunks: int, on_chunk) -> None:
        self._chunks = chunks
        self._on_chunk = on_chunk

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def iter_bytes(self, chunk_size: int):
        for index in range(self._chunks):
            self._on_chunk(index)
            yield b"x"


def _testland_region() -> Region:
    return Region(
        id=REGION,
        name=REGION,
        south=45.0,
        west=12.0,
        north=46.0,
        east=13.0,
        pbf_url="https://example.test/testland.osm.pbf",
    )


def test_a_cancelled_download_stops_and_removes_its_partial_file(
    cached_extract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cancel = threading.Event()
    handed_out: list[int] = []

    def on_chunk(index: int) -> None:
        handed_out.append(index)
        if index == 2:
            cancel.set()

    monkeypatch.setattr(extract_module, "find_region", lambda _: _testland_region())
    monkeypatch.setattr(
        extract_module.httpx, "stream", lambda *a, **k: _FakeStream(100, on_chunk)
    )

    with pytest.raises(ExtractCancelled):
        download_extract(REGION, cancel=cancel)

    # stopped where the flag was set, not after all 100 chunks.
    assert len(handed_out) == 3
    partial = cached_extract.with_suffix(cached_extract.suffix + ".part")
    assert not partial.exists()
    assert not cached_extract.exists()


def test_an_uncancelled_download_completes(
    cached_extract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """the same fake, never cancelled."""
    monkeypatch.setattr(extract_module, "find_region", lambda _: _testland_region())
    monkeypatch.setattr(
        extract_module.httpx, "stream", lambda *a, **k: _FakeStream(5, lambda _: None)
    )

    path = download_extract(REGION, cancel=threading.Event())

    assert path == cached_extract
    assert path.read_bytes() == b"xxxxx"


def test_a_cancelled_sync_cleans_up_instead_of_failing(
    db_session: Session,
    sync_session_factory: sessionmaker[Session],
    venice: Destination,
    cached_extract: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nothing left stuck at "downloading", and no failure message."""
    import app.sync as sync_module

    venice.region = REGION
    db_session.commit()

    def cancelled_download(region: str, cancel: threading.Event) -> Path:
        # the cancel flag is registered while downloading.
        assert sync_module._downloads.get(region) is cancel
        raise ExtractCancelled("cancelled")

    monkeypatch.setattr(sync_module, "download_extract", cancelled_download)

    sync_destination(venice.id, session_factory=sync_session_factory)

    db_session.expire_all()
    assert db_session.scalar(
        select(OsmExtract).where(OsmExtract.region == REGION)
    ) is None
    # and unregistered afterwards.
    assert REGION not in sync_module._downloads

    refreshed = db_session.get(Destination, venice.id)
    assert refreshed is not None
    assert refreshed.sync_state == SyncState.FAILED
    assert refreshed.sync_error == "The download was cancelled."


def test_a_sync_cancelled_because_its_destination_was_deleted(
    db_session: Session,
    sync_session_factory: sessionmaker[Session],
    venice: Destination,
    cached_extract: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """the destination is deleted during the download.

    regression test for a StaleDataError when writing to the stale copy.
    """
    import app.sync as sync_module

    venice.region = REGION
    db_session.commit()
    venice_id = venice.id

    def deleted_mid_download(region: str, cancel: threading.Event) -> Path:
        # another session (the delete request) removes it mid-download.
        db_session.delete(db_session.get(Destination, venice_id))
        db_session.commit()
        raise ExtractCancelled("cancelled")

    monkeypatch.setattr(sync_module, "download_extract", deleted_mid_download)

    sync_destination(venice_id, session_factory=sync_session_factory)

    db_session.expire_all()
    assert db_session.get(Destination, venice_id) is None
    assert db_session.scalar(
        select(OsmExtract).where(OsmExtract.region == REGION)
    ) is None
