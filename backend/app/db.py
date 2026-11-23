"""database engine, session factory and the per-request session dependency."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

# pre-ping so a restarted postgres container doesn't leave stale pooled connections.
engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

# routes serialise objects after commit, so keep them readable instead of expiring them.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    """base class for all orm models."""


def get_db() -> Generator[Session, None, None]:
    """one session per request, closed even if the route raises."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
