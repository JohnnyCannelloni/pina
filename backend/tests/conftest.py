"""test fixtures.

integration tests against a real postgres (pina_test), with the schema built by
the real migrations and requests going through fastapi's TestClient. each test
runs in a transaction that is rolled back. the geocoder, sync runner and
overpass client are faked, so nothing leaves the machine.
"""

from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import settings
from app.db import get_db
from app.main import app
from app.geocode import GeocodeError, GeocodeResult, get_geocoder
from app.models import Destination
from app.seed import seed
from app.sync import get_sync_runner

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def engine() -> Generator[Engine, None, None]:
    """migrate the test database once per run and return an engine."""
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # env.py reads this, so the suite never migrates the real database.
    alembic_cfg.set_main_option("sqlalchemy.url", settings.TEST_DATABASE_URL)

    # rebuild from scratch each run; also exercises downgrade().
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")

    test_engine = create_engine(settings.TEST_DATABASE_URL, pool_pre_ping=True)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def db_connection(engine: Engine) -> Generator[Connection, None, None]:
    """one connection per test, inside a transaction that's always rolled back.

    shared by the request session and the session a background sync opens.
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def _session_factory(connection: Connection) -> sessionmaker[Session]:
    """sessions on the test connection whose commits become savepoints.

    the code under test really commits, and the outer rollback still undoes it.
    """
    return sessionmaker(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
def db_session(db_connection: Connection) -> Generator[Session, None, None]:
    """the session request handlers get through the get_db override."""
    session = _session_factory(db_connection)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sync_session_factory(db_connection: Connection) -> sessionmaker[Session]:
    """a session factory for sync_destination, bound to the test connection."""
    return _session_factory(db_connection)


@pytest.fixture
def venice(db_session: Session) -> Destination:
    """italy -> venice, created by the real seed."""
    destination = seed(db_session)
    db_session.commit()
    return destination


class FakeGeocoder:
    """stands in for NominatimClient. fakes only the network."""

    def __init__(self) -> None:
        self.results: list[GeocodeResult] = []
        self.error: GeocodeError | None = None
        self.queries: list[str] = []
        self.viewboxes: list[tuple[float, float, float, float] | None] = []

    def search(
        self,
        query: str,
        viewbox: tuple[float, float, float, float] | None = None,
        limit: int = 5,
    ) -> list[GeocodeResult]:
        self.queries.append(query)
        self.viewboxes.append(viewbox)
        if self.error is not None:
            raise self.error
        return self.results


@pytest.fixture
def fake_geocoder() -> Generator[FakeGeocoder, None, None]:
    """replace the nominatim client for one test."""
    fake = FakeGeocoder()
    app.dependency_overrides[get_geocoder] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_geocoder, None)


class FakeOverpass:
    """stands in for OverpassClient. returns `elements`, or raises `error`.

    `on_fetch` runs before returning, so a test can change the database
    mid-sync (e.g. delete the destination being synced).
    """

    def __init__(self) -> None:
        self.elements: list[dict] = []
        self.error: Exception | None = None
        self.boxes: list[tuple[float, float, float, float]] = []
        self.on_fetch: Callable[[], None] | None = None

    def fetch(
        self, south: float, west: float, north: float, east: float
    ) -> list[dict]:
        self.boxes.append((south, west, north, east))
        if self.on_fetch is not None:
            self.on_fetch()
        if self.error is not None:
            raise self.error
        return self.elements


@pytest.fixture
def fake_overpass() -> FakeOverpass:
    """a fresh fake overpass client; pass it to sync_destination."""
    return FakeOverpass()


class RecordingSyncRunner:
    """records that a sync was scheduled, without running it.

    the sync itself is tested in test_sync.py.
    """

    def __init__(self) -> None:
        self.started: list[int] = []

    def __call__(self, destination_id: int) -> None:
        self.started.append(destination_id)


@pytest.fixture
def sync_runner() -> Generator[RecordingSyncRunner, None, None]:
    runner = RecordingSyncRunner()
    app.dependency_overrides[get_sync_runner] = lambda: runner
    yield runner
    app.dependency_overrides.pop(get_sync_runner, None)


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """a TestClient wired to the rolled-back session."""

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)
