"""background sync: fill a destination's places with one overpass query.

pending -> syncing -> ready, or failed with a readable sync_error. the fetching
and parsing live in app/overpass.py.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.models import Destination, SyncState
from app.overpass import (
    MAX_SPAN_DEGREES,
    OverpassError,
    check_box,
    get_overpass,
    parse_elements,
    store_places,
)

logger = logging.getLogger(__name__)

#: new destinations are clamped to this per side, so they always fit in one
#: overpass query.
MAX_DESTINATION_SPAN_DEGREES = MAX_SPAN_DEGREES

#: shown when a sync fails for a reason other than overpass itself.
UNEXPECTED_ERROR = "Something went wrong while saving places. Try again."


# destinations currently syncing, so a double click can't start two jobs.
# fine for one process; multiple workers would need a row lock.
_active_lock = threading.Lock()
_active: set[int] = set()


class PlaceSource(Protocol):
    """what the sync needs from an overpass client. tests pass a fake."""

    def fetch(
        self, south: float, west: float, north: float, east: float
    ) -> list[dict]: ...


def clamp_area(
    latitude: float,
    longitude: float,
    south: float,
    west: float,
    north: float,
    east: float,
) -> tuple[float, float, float, float]:
    """shrink an oversized box around the centre. returns (south, west, north, east)."""
    half = MAX_DESTINATION_SPAN_DEGREES / 2

    if north - south > MAX_DESTINATION_SPAN_DEGREES:
        south, north = latitude - half, latitude + half
    if east - west > MAX_DESTINATION_SPAN_DEGREES:
        west, east = longitude - half, longitude + half

    return south, west, north, east


def _covering_destination(db: Session, destination: Destination) -> Destination | None:
    """another synced destination whose box fully contains this one, if any.

    osm_places is shared by every destination and user, so a city someone
    already loaded needs no second query.
    """
    return db.scalar(
        select(Destination)
        .where(
            Destination.id != destination.id,
            Destination.sync_state == SyncState.READY,
            Destination.south <= destination.south,
            Destination.west <= destination.west,
            Destination.north >= destination.north,
            Destination.east >= destination.east,
        )
        .limit(1)
    )


def sync_destination(
    destination_id: int,
    session_factory: sessionmaker[Session] | None = None,
    overpass: PlaceSource | None = None,
) -> None:
    """fill the place cache for a destination's box.

    runs as a background task after the response is sent, so it opens its own
    session. `session_factory` and `overpass` let tests keep it inside their
    transaction and off the network. never raises: failures are recorded on
    the destination instead.
    """
    make_session = session_factory or SessionLocal
    client = overpass or get_overpass()

    with _active_lock:
        if destination_id in _active:
            logger.info("sync already running for destination %s", destination_id)
            return
        _active.add(destination_id)

    try:
        with make_session() as db:
            destination = db.get(Destination, destination_id)
            if destination is None:
                logger.warning("sync: destination %s is gone", destination_id)
                return

            covering = _covering_destination(db, destination)
            if covering is not None:
                destination.sync_state = SyncState.READY
                destination.sync_error = None
                destination.synced_at = datetime.now(UTC)
                db.commit()
                logger.info(
                    "sync destination %s ready: already covered by destination %s",
                    destination_id,
                    covering.id,
                )
                return

            destination.sync_state = SyncState.SYNCING
            destination.sync_error = None
            db.commit()

            box = (
                destination.south,
                destination.west,
                destination.north,
                destination.east,
            )

            try:
                check_box(*box)
                rows = parse_elements(client.fetch(*box))
                written = store_places(db, rows)

                # the destination may have been deleted while we were fetching.
                destination = _reload_destination(db, destination_id)
                if destination is not None:
                    destination.sync_state = SyncState.READY
                    destination.sync_error = None
                    destination.synced_at = datetime.now(UTC)
                db.commit()
            except OverpassError as exc:
                logger.warning("sync destination %s failed: %s", destination_id, exc)
                _mark_failed(db, destination_id, str(exc))
                return
            except Exception:
                # anything else (a database error, a bug) must not leave the
                # destination stuck on syncing.
                logger.exception("sync destination %s crashed", destination_id)
                _mark_failed(db, destination_id, UNEXPECTED_ERROR)
                return

            logger.info(
                "sync destination %s ready: %s places", destination_id, written
            )
    finally:
        # always release the claim, even after a crash.
        with _active_lock:
            _active.discard(destination_id)


def _mark_failed(db: Session, destination_id: int, message: str) -> None:
    """record a failed sync, retryable from the ui. the cache keeps what it had."""
    try:
        db.rollback()
        destination = _reload_destination(db, destination_id)
        if destination is not None:
            destination.sync_state = SyncState.FAILED
            destination.sync_error = message
        db.commit()
    except Exception:
        # the database itself is unreachable; nothing more we can record.
        logger.exception("could not mark destination %s as failed", destination_id)


def _reload_destination(db: Session, destination_id: int) -> Destination | None:
    """reload the destination from the database, or None if it was deleted.

    db.get() would return the stale copy from the start of the sync, and
    writing to a deleted row fails the commit with StaleDataError.
    """
    return db.scalar(select(Destination).where(Destination.id == destination_id))


def get_sync_runner() -> Callable[[int], None]:
    """fastapi dependency returning the sync function. tests swap in a recorder."""
    return sync_destination
