"""Test fixtures.

The interesting decision here is that these are *integration* tests: they talk
to a real Postgres, through the real migration, over real HTTP routing. Three
things could have been faked and were not:

1. **The database.** Not SQLite. SQLite has no CHECK-constraint parity worth
   relying on, no TIMESTAMPTZ, and no DOUBLE PRECISION semantics — so a green
   SQLite suite would say nothing about whether Postgres accepts the data.
2. **The schema.** Built by `alembic upgrade head`, not
   `Base.metadata.create_all()`. create_all reads the models, so it would test
   the models against themselves and pass even if the migration were broken.
   Running the migration means a mistake in 0001_initial.py fails the suite —
   which is the entire reason to have the test.
3. **The app.** A real FastAPI TestClient, so path parsing, Pydantic
   validation, and status codes are all exercised.

What *is* swapped: the database URL (pina_test, not pina), the get_db dependency
(a transaction that gets rolled back), the geocoder, and the sync runner. So the
suite never touches the places you saved by hand, and never makes an outbound
request — no geocode, and above all no 600 MB download.
"""

from collections.abc import Generator
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
    """Migrate the test database once per run, then hand back an engine."""
    alembic_cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # env.py's get_url() reads this and migrates pina_test instead of pina.
    # Without this line the suite would migrate — and roll back against — your
    # real database.
    alembic_cfg.set_main_option("sqlalchemy.url", settings.TEST_DATABASE_URL)

    # Rebuild from scratch, so the suite cannot pass against a schema left over
    # from an older version of the migration. This also exercises downgrade(),
    # which is otherwise the kind of code nobody runs until they need it badly.
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")

    test_engine = create_engine(settings.TEST_DATABASE_URL, pool_pre_ping=True)
    yield test_engine
    test_engine.dispose()


@pytest.fixture
def db_connection(engine: Engine) -> Generator[Connection, None, None]:
    """One connection per test, inside a transaction that is always rolled back.

    The connection is a fixture of its own (rather than a local in db_session)
    because two different things need to share it: the request session, and the
    session the background sync opens for itself. Binding both to this
    connection is what lets a sync run inside the test's transaction and still
    be discarded at the end.
    """
    connection = engine.connect()
    transaction = connection.begin()
    try:
        yield connection
    finally:
        transaction.rollback()
        connection.close()


def _session_factory(connection: Connection) -> sessionmaker[Session]:
    """Sessions bound to the test's connection, whose commits are savepoints.

    The trick: the transaction is open on the *connection*, outside the Session,
    and `join_transaction_mode="create_savepoint"` makes Session.commit() release
    a SAVEPOINT rather than commit for real. So the code under test calls
    commit() genuinely — it has to, that is what is being tested — while the
    outer transaction stays open and the final rollback erases everything.

    This is why the tests are order-independent and leave no rows behind without
    a TRUNCATE between them.
    """
    return sessionmaker(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        autoflush=False,
    )


@pytest.fixture
def db_session(db_connection: Connection) -> Generator[Session, None, None]:
    """The Session the request handlers get, via the get_db override."""
    session = _session_factory(db_connection)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sync_session_factory(db_connection: Connection) -> sessionmaker[Session]:
    """A session factory for `sync_destination` to use as its own.

    Without this, a sync started in a test would call the real `SessionLocal`,
    which is bound to the development database — so the test would write rows
    into your actual data and see none of the test's own setup. Handing it a
    factory on the test connection keeps the whole job inside the rolled-back
    transaction.
    """
    return _session_factory(db_connection)


@pytest.fixture
def venice(db_session: Session) -> Destination:
    """Italy -> Venice, seeded by the same app.seed the real app uses.

    Reusing the production seed rather than hand-building rows means the test
    exercises the seed too; if seed() stops producing a valid destination, these
    tests notice.
    """
    destination = seed(db_session)
    db_session.commit()
    return destination


class FakeGeocoder:
    """Stands in for NominatimClient. Fakes only the network."""

    def __init__(self) -> None:
        self.results: list[GeocodeResult] = []
        self.error: GeocodeError | None = None
        self.queries: list[str] = []

    def search(self, query: str) -> list[GeocodeResult]:
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.results


@pytest.fixture
def fake_geocoder() -> Generator[FakeGeocoder, None, None]:
    """Replace the Nominatim client for one test."""
    fake = FakeGeocoder()
    app.dependency_overrides[get_geocoder] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_geocoder, None)


class RecordingSyncRunner:
    """Records that a sync was scheduled, without running one.

    Used by the endpoint tests, which care that creating a destination *starts*
    a fill — not what the fill does. Letting the real one run would make those
    tests download a few hundred megabytes of OpenStreetMap data.

    The import's actual behaviour is tested directly in test_extract.py against
    a tiny checked-in fixture. Two different questions, two different tools.
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
    """A TestClient wired to the rolled-back session.

    dependency_overrides is FastAPI's seam for exactly this: the routes keep
    asking for get_db and have no idea they are being handed a test session.
    No conditional "if testing" branch anywhere in the app code.
    """

    def override_get_db() -> Generator[Session, None, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)
