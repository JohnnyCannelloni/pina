"""background sync: resolve a destination's region, download and import it.

the downloading and parsing live in app/extract.py; this module drives them
and records progress on the destination and extract rows for the ui.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.db import SessionLocal
from app.extract import (
    ExtractCancelled,
    ExtractError,
    download_extract,
    extract_path,
    extract_state,
    import_extract,
)
from app.models import Destination, ExtractState, OsmExtract, SyncState
from app.regions import UnsupportedRegion, find_region, resolve_region

logger = logging.getLogger(__name__)

#: sanity limit per side, in degrees (~220 km). the stored area is only the
#: map's opening view, so this just rejects continent-sized geocoder boxes.
MAX_DESTINATION_SPAN_DEGREES = 2.0


# destinations currently syncing, so a double click can't start two jobs.
# fine for one process; multiple workers would need a row lock.
_active_lock = threading.Lock()
_active: set[int] = set()

# cancel flag per region download. a fresh event per sync, so re-adding a city
# doesn't inherit an old cancellation. guarded by _active_lock.
_downloads: dict[str, threading.Event] = {}


def cancel_download(region: str) -> bool:
    """stop a region's download if one is running. returns whether it was.

    an import that's already running is left to finish.
    """
    with _active_lock:
        event = _downloads.get(region)
    if event is None:
        return False
    event.set()
    logger.info("cancelling download of %s", region)
    return True


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


def region_for_destination(destination: Destination) -> str:
    """the destination's geofabrik region, resolved from its coordinates.

    raises UnsupportedRegion when nothing covers the point.
    """
    if destination.region:
        return destination.region
    return resolve_region(destination.latitude, destination.longitude).id


def sync_destination(
    destination_id: int,
    session_factory: sessionmaker[Session] | None = None,
) -> None:
    """download and import the extract covering a destination.

    runs as a background task after the response is sent, so it opens its own
    session. `session_factory` lets tests keep it inside their transaction.
    never raises: failures are recorded on the destination instead.
    """
    make_session = session_factory or SessionLocal

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

            try:
                region = region_for_destination(destination)
            except UnsupportedRegion as exc:
                destination.sync_state = SyncState.FAILED
                destination.sync_error = str(exc)
                db.commit()
                return

            # store the region so later lookups can't repoint it.
            destination.region = region
            destination.sync_state = SyncState.SYNCING
            destination.sync_error = None
            db.commit()

            extract = extract_state(db, region)

            try:
                # download, unless the file is already on disk.
                if not extract_path(region).exists():
                    extract.state = ExtractState.DOWNLOADING
                    extract.error = None
                    db.commit()

                cancel = threading.Event()
                with _active_lock:
                    _downloads[region] = cancel
                try:
                    path = download_extract(region, cancel=cancel)
                finally:
                    with _active_lock:
                        # only remove our own flag; a newer sync may have replaced it.
                        if _downloads.get(region) is cancel:
                            del _downloads[region]
                extract.size_bytes = path.stat().st_size

                # import
                extract.state = ExtractState.IMPORTING
                db.commit()

                # the whole region, so every viewport inside it is served.
                written = import_extract(db, region)

                # store the bounds so coverage can tell "empty" from "not imported".
                bounds = find_region(region)
                extract.south = bounds.south
                extract.west = bounds.west
                extract.north = bounds.north
                extract.east = bounds.east

                siblings = list(
                    db.scalars(
                        select(Destination)
                        .where(Destination.region == region)
                        .order_by(Destination.id)
                    )
                )

                extract.state = ExtractState.READY
                extract.place_count = written
                extract.imported_at = datetime.now(UTC)
                extract.error = None
                extract.updated_at = datetime.now(UTC)

                # every destination in the region is filled now.
                for sibling in siblings:
                    sibling.sync_state = SyncState.READY
                    sibling.sync_error = None
                    sibling.synced_at = datetime.now(UTC)

                db.commit()
                logger.info(
                    "sync destination %s ready: %s places from %s",
                    destination_id,
                    written,
                    region,
                )

            except ExtractCancelled:
                # nothing needs this region any more. drop the unfinished
                # tracking row so it doesn't look stuck on "downloading".
                db.rollback()
                unfinished = db.scalar(
                    select(OsmExtract).where(
                        OsmExtract.region == region,
                        OsmExtract.state != ExtractState.READY,
                    )
                )
                if unfinished is not None:
                    db.delete(unfinished)

                # usually the destination is already gone. if not, mark it failed.
                destination = _reload_destination(db, destination_id)
                if destination is not None:
                    destination.sync_state = SyncState.FAILED
                    destination.sync_error = "The download was cancelled."
                db.commit()
                logger.info("sync destination %s cancelled", destination_id)

            except ExtractError as exc:
                # retryable: the next attempt resumes the partial download.
                db.rollback()
                extract = extract_state(db, region)
                extract.state = ExtractState.FAILED
                extract.error = str(exc)
                extract.updated_at = datetime.now(UTC)

                destination = _reload_destination(db, destination_id)
                if destination is not None:
                    destination.sync_state = SyncState.FAILED
                    destination.sync_error = str(exc)
                db.commit()
                logger.warning("sync destination %s failed: %s", destination_id, exc)
    finally:
        # always release the claim, even after a crash.
        with _active_lock:
            _active.discard(destination_id)


def _reload_destination(db: Session, destination_id: int) -> Destination | None:
    """reload the destination from the database, or None if it was deleted.

    db.get() would return the stale copy from the start of the sync, and
    writing to a deleted row fails the commit with StaleDataError.
    """
    return db.scalar(select(Destination).where(Destination.id == destination_id))


def get_sync_runner() -> Callable[[int], None]:
    """fastapi dependency returning the sync function. tests swap in a recorder."""
    return sync_destination
