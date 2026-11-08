"""Database engine, session factory, and the FastAPI dependency that hands a
session to a route.

Three distinct things live here, and it is worth keeping them straight because
the names are similar:

- **Engine**: the connection pool. One per process, created at import. It does
  not open a connection until something asks for one.
- **Session**: a unit of work. One per request. It tracks the objects you have
  added or modified and flushes them as SQL when you commit.
- **get_db**: the dependency that creates a Session for one request and
  guarantees it gets closed, so connections return to the pool.
"""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# pool_pre_ping checks a pooled connection is still alive before handing it out.
# Without it, restarting the Postgres container while the API is running gives
# you one confusing "server closed the connection unexpectedly" error per stale
# pooled connection. Cheap insurance locally, standard practice in production.
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

# expire_on_commit=False: after session.commit(), SQLAlchemy would normally mark
# the object's attributes as stale so the next access re-fetches them. The route
# commits and *then* serialises the new place to JSON, which would trigger that
# extra SELECT — and would fail outright once the session is closed. Turning it
# off lets the committed object stay readable.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """Parent of every ORM model. Collects table metadata in Base.metadata."""


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one Session per request, always closed.

    The `yield` is the point of this function. Everything before it is setup,
    everything after it is teardown that runs once the response has been
    produced — even if the route raised. That is what stops a failing request
    from leaking a connection out of the pool.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
