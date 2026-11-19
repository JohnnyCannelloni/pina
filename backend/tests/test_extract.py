"""Tests for region resolution and the extract importer.

No network: `regions.py` is tested against a small literal index fixture, and
imports against a real `.osm.pbf` written by osmium at test time. So the
polygon maths, the tag mapping and the upsert are all production code paths —
only the 476 MB download is absent.
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
    """Write a real .osm.pbf containing the given nodes.

    `nodes` are (id, latitude, longitude, tags). Note osmium's Node location is
    (lon, lat) — the opposite order from everything else here, which is exactly
    the sort of thing worth pinning down in a helper once.
    """
    # SimpleWriter refuses to open an existing file, and the idempotency test
    # deliberately writes the same path twice.
    path.unlink(missing_ok=True)
    with osmium.SimpleWriter(str(path)) as writer:
        for node_id, latitude, longitude, tags in nodes:
            writer.add_node(
                osmium.osm.mutable.Node(
                    id=node_id, location=(longitude, latitude), tags=tags
                )
            )


def square(west: float, south: float, east: float, north: float) -> list:
    """A closed GeoJSON ring, in [longitude, latitude] order."""
    return [
        [west, south],
        [east, south],
        [east, north],
        [west, north],
        [west, south],
    ]


@pytest.fixture
def cached_extract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the extract cache at a temp dir and return the fixture's path."""
    monkeypatch.setattr(
        extract_module.settings, "EXTRACT_CACHE_DIR", str(tmp_path)
    )
    return extract_module.extract_path(REGION)


@pytest.fixture
def fake_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tiny Geofabrik-shaped index: a country with one nested sub-region.

    Deliberately shaped like the real case that broke: a big region and a
    smaller one inside it, so "smallest containing region wins" is actually
    exercised rather than assumed.
    """
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
                # An L-shaped region whose *bounding box* covers (2.5, 2.5) but
                # whose actual shape does not. This is the case that made the
                # bounding-box-only version put New York City in New Jersey.
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
    # Clear the process-level cache, or a previous test's index would be reused.
    monkeypatch.setattr(regions_module, "_regions", None)


# --- region resolution -----------------------------------------------------


def test_resolve_picks_the_smallest_containing_region(fake_index: None) -> None:
    """The whole New York fix, in one assertion.

    A point inside both the country and its sub-region must resolve to the
    sub-region. In the real index that is the difference between
    north-america/us/new-york at 476 MB and north-america/us at 11,706 MB.
    """
    # (1.5, 1.5) is well inside smallstate and well outside the L-shape.
    # Avoid a point that lands exactly on a polygon vertex: whether a boundary
    # point counts as inside is genuinely undefined for ray casting, so a test
    # sitting on one is testing an implementation detail, not behaviour.
    chosen = resolve_region(1.5, 1.5)
    assert chosen.id == "bigland/smallstate"

    # And the country is genuinely a candidate — the test would be vacuous if
    # only one region contained the point.
    assert [region.id for region in candidates_for(1.5, 1.5)] == [
        "bigland/smallstate",
        "bigland",
    ]


def test_resolve_falls_back_to_the_country(fake_index: None) -> None:
    """Somewhere with no sub-region still resolves."""
    assert resolve_region(8.0, 8.0).id == "bigland"


def test_resolve_uses_real_geometry_not_the_bounding_box(
    fake_index: None,
) -> None:
    """A point in a region's bbox but outside its shape is excluded.

    This is the regression test for a measured failure: with a bounding-box
    test, New York City resolved to `us/new-jersey` (New Jersey is a thin
    diagonal state whose box covers Manhattan) and Lisbon resolved to `spain`.
    Two of five test cities landed in the wrong state or country.

    Here, (2.5, 2.5) sits in the notch of the L-shaped region — inside its
    bounding box, outside its polygon. The smaller bbox area means a
    bbox-only implementation would prefer it and be wrong.
    """
    lshape = find_region("bigland/lshape")
    assert lshape.in_bounds(2.5, 2.5)  # the bbox does contain the point
    assert not lshape.contains(2.5, 2.5)  # the shape does not

    assert resolve_region(2.5, 2.5).id == "bigland/smallstate"


def test_resolve_raises_when_nothing_contains_the_point(
    fake_index: None,
) -> None:
    """Mid-ocean raises rather than returning None.

    Returning None would let a caller read "no map data here" as "no
    restaurants here" — a silent empty map, the failure mode this whole feature
    has been fighting.
    """
    with pytest.raises(UnsupportedRegion):
        resolve_region(-40.0, -140.0)


def test_region_url_comes_from_the_index(fake_index: None) -> None:
    """The download URL is read, not constructed.

    In the real index a region's id and its URL path differ — "us/new-york" is
    published under north-america/ — so building the URL from the id would be
    wrong exactly where it matters most.
    """
    assert (
        find_region("bigland/smallstate").pbf_url
        == "https://example.test/smallstate.osm.pbf"
    )


def test_multipolygon_and_polygon_are_both_handled(fake_index: None) -> None:
    """The index mixes both geometry types, nested one level apart."""
    assert find_region("bigland").contains(5.0, 5.0)  # Polygon
    assert find_region("bigland/smallstate").contains(1.5, 1.5)  # MultiPolygon


# --- area clamping ---------------------------------------------------------


def test_clamp_area_leaves_new_york_city_alone() -> None:
    """NYC's real bounding box must survive unclamped.

    The regression test for the fence: at the old 0.12-degree limit, NYC's
    0.44 x 0.56 box was cut to a 13 km square, so panning across the city
    simply stopped producing markers.
    """
    south, west, north, east = clamp_area(
        40.7128, -74.0060, 40.4774, -74.2591, 40.9176, -73.7004
    )
    assert north - south == pytest.approx(0.4402, abs=1e-3)
    assert east - west == pytest.approx(0.5587, abs=1e-3)


def test_clamp_area_still_rejects_a_continent() -> None:
    """The loose sanity limit remains."""
    south, west, north, east = clamp_area(0.0, 0.0, -45.0, -90.0, 45.0, 90.0)
    assert north - south <= MAX_DESTINATION_SPAN_DEGREES + 1e-9
    assert east - west <= MAX_DESTINATION_SPAN_DEGREES + 1e-9


# --- importing -------------------------------------------------------------


def test_import_maps_tags_and_drops_junk(
    db_session: Session, cached_extract: Path
) -> None:
    """Each OSM tag lands in the right category; unusable nodes are skipped."""
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
    # Provenance, so a region can be refreshed or dropped wholesale.
    assert {row.region for row in rows} == {REGION}


def test_import_keeps_the_whole_region(
    db_session: Session, venice: Destination, cached_extract: Path
) -> None:
    """Places far outside any destination are kept.

    The opposite of the previous behaviour, and the point of migration 0005: a
    place 500 km from Venice is still cached, so a destination added in Rome
    tomorrow needs no re-import, and panning out of Venice does not hit a fence.
    """
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
    """Re-importing updates in place rather than duplicating.

    This is the ON CONFLICT DO UPDATE in _flush, now keyed on source_ref alone.
    DO NOTHING would keep the stale name forever; no clause at all would raise a
    unique violation on the second import and make refreshing impossible.
    """
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
    """A missing extract is a clear error, not an empty success."""
    with pytest.raises(ExtractError, match="not been downloaded"):
        import_extract(db_session, REGION)


# --- the orchestrating sync ------------------------------------------------


def test_sync_imports_and_records_coverage(
    db_session: Session,
    sync_session_factory: sessionmaker[Session],
    venice: Destination,
    cached_extract: Path,
    fake_index: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full run fills the cache, marks ready, and records where it covers.

    The coverage bounds are the part worth asserting: without them `/nearby`
    cannot tell an empty area from an unimported one.
    """
    # Point the destination at the fixture region, and make find_region resolve
    # it — the fake index knows nothing about "europe/testland".
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
    # Coverage recorded, so /nearby can answer "covered" rather than guessing.
    assert (extract.south, extract.north) == (45.0, 46.0)


def test_sync_reports_an_unsupported_location_without_touching_saved_places(
    db_session: Session,
    sync_session_factory: sessionmaker[Session],
    venice: Destination,
    fake_index: None,
    client,
) -> None:
    """A location no region covers fails clearly and harmlessly.

    The second half is the guarantee that matters: losing suggestions must never
    cost you your own data. `places` and `osm_places` being separate tables is
    what makes that true structurally.
    """
    saved = client.post(
        f"/api/destinations/{venice.id}/places",
        json={"name": "My Own Place", "category": "cafe"},
    )
    assert saved.status_code == 201, saved.text

    venice.region = None
    # Mid-Pacific: the fixture index covers only 0..10 degrees.
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


# --- cancelling a download -------------------------------------------------


class _FakeStream:
    """Stands in for httpx.stream: a 200 response of `chunks` 1-byte chunks.

    `on_chunk` runs as each chunk is handed out, which lets a test flip the
    cancel flag partway through — the situation a real cancel is in.
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

    # Stopped at the chunk where the flag was set, not after all 100.
    assert len(handed_out) == 3
    partial = cached_extract.with_suffix(cached_extract.suffix + ".part")
    assert not partial.exists()
    assert not cached_extract.exists()


def test_an_uncancelled_download_completes(
    cached_extract: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same fake, never cancelled: proves the check does not misfire."""
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
    """No row left stuck at 'downloading', and no retryable-failure message."""
    import app.sync as sync_module

    venice.region = REGION
    db_session.commit()

    def cancelled_download(region: str, cancel: threading.Event) -> Path:
        # While "downloading", the sync must have registered its cancel flag.
        assert sync_module._downloads.get(region) is cancel
        raise ExtractCancelled("cancelled")

    monkeypatch.setattr(sync_module, "download_extract", cancelled_download)

    sync_destination(venice.id, session_factory=sync_session_factory)

    db_session.expire_all()
    assert db_session.scalar(
        select(OsmExtract).where(OsmExtract.region == REGION)
    ) is None
    # The flag is unregistered once the sync is over.
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
    """The real sequence: the destination is deleted *during* the download.

    The sync's own session still holds the destination it loaded at the start.
    This caught a StaleDataError in the first version, which wrote "cancelled"
    onto that stale copy and lost the cleanup with the failed commit.
    """
    import app.sync as sync_module

    venice.region = REGION
    db_session.commit()
    venice_id = venice.id

    def deleted_mid_download(region: str, cancel: threading.Event) -> Path:
        # Another session — the DELETE request — removes it while we download.
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
