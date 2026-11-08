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

What *is* swapped: the database URL (pina_test, not pina) and the get_db
dependency (a transaction that gets rolled back). So running the suite never
touches the places you saved by hand.
"""

from collections.abc import Generator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.main import app
from app.models import Destination
from app.seed import seed

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
def db_session(engine: Engine) -> Generator[Session, None, None]:
    """A Session whose writes are discarded when the test ends.

    The trick: open a transaction on the connection *outside* the Session, then
    tell the Session to use a SAVEPOINT when it commits. The route under test
    calls db.commit() for real — it has to, that is what is being tested — but
    that only releases the savepoint. The outer transaction is still open, and
    rolling it back at the end removes everything the test wrote.

    This is why the tests are order-independent and leave no rows behind,
    without a TRUNCATE between them.
    """
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
        autoflush=False,
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


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
    # Clear the override, or it leaks into any later test module.
    app.dependency_overrides.clear()
