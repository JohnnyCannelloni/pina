"""Filling a destination's place cache, in the background.

READ THIS THIRD, after migration 0004 and app/extract.py.

This is the orchestration layer: it decides *which* extract a destination
needs, drives the download and import, and records what happened so the UI can
say something honest. The actual downloading and parsing is in extract.py.

The shape of the job:

    destination (Lisbon, country 'pt')
      -> region 'europe/portugal'                     app/regions.py
      -> download europe/portugal-latest.osm.pbf      extract.download_extract
         (skipped entirely if already on disk)
      -> parse it, keeping places inside the bbox of
         EVERY destination in that region              extract.import_extract
      -> upsert into osm_places                        -> the map reads this

Adding Porto afterwards re-parses the cached file in seconds and downloads
nothing.

**Two things worth taking from this file:**

1. *A background task must own its session.* See `sync_destination`.
2. *An area clamp is still needed*, even though the data source is now local —
   but for a different reason. See `clamp_area`.
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

#: Loose sanity limit on a destination's stored area, per side, in degrees.
#: ~220 km.
#:
#: This number has now had three different justifications, which is worth
#: noticing. It began as "do not ask a shared API for too much". Then it became
#: "do not cache too many markers". **Both are now obsolete**, and keeping the
#: tight 0.12 value caused a real bug: New York City's own bounding box is
#: 0.44 x 0.56 degrees, so the destination's area was a *fence* — pan more than
#: ~13 km and the markers stopped, with no explanation.
#:
#: A destination's area is now only the map's opening view. Imports cover a
#: whole Geofabrik region, and the viewport query is bounded by what is on
#: screen, so nothing needs this to be small. All that is left is rejecting a
#: geocoder result so large it would open the map on a whole continent.
#:
#: The lesson, for the second time in this project: a limit whose reason has
#: gone is a bug waiting to fire. Delete it or re-justify it; never just keep
#: the number.
MAX_DESTINATION_SPAN_DEGREES = 2.0


# Which destinations are mid-sync, so a double-click cannot start two jobs that
# fight over the same rows. One process, so a set plus a lock is enough; with
# multiple workers this would need a row lock on the destination.
_active_lock = threading.Lock()
_active: set[int] = set()

# The cancel flag of each download in progress, by region. A fresh Event per
# sync rather than a set of "cancelled regions": re-adding a city after
# cancelling it must start a clean download, not inherit the old cancellation.
# Guarded by _active_lock.
_downloads: dict[str, threading.Event] = {}


def cancel_download(region: str) -> bool:
    """Stop a region's download, if one is running. Returns whether one was.

    Only the download stops. An import already under way finishes: it takes
    seconds, and the places it writes are a cache that is useful to keep.
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
    """Shrink an oversized bounding box around the destination's centre.

    Returns (south, west, north, east). A box already within the limit is
    returned unchanged, so a small town keeps its real extent.
    """
    half = MAX_DESTINATION_SPAN_DEGREES / 2

    if north - south > MAX_DESTINATION_SPAN_DEGREES:
        south, north = latitude - half, latitude + half
    if east - west > MAX_DESTINATION_SPAN_DEGREES:
        west, east = longitude - half, longitude + half

    return south, west, north, east


def region_for_destination(destination: Destination) -> str:
    """The Geofabrik region a destination needs, stored on first resolution.

    Resolved from the destination's *coordinates*, not its country code: that
    is what picks `us/new-york` (476 MB) over `us` (11,706 MB). See
    app/regions.py.

    Raises UnsupportedRegion when no region contains the point, which the caller
    turns into a clear message. The destination stays usable — you can still
    save places by hand — it just cannot auto-fill.
    """
    if destination.region:
        return destination.region
    return resolve_region(destination.latitude, destination.longitude).id


def sync_destination(
    destination_id: int,
    session_factory: sessionmaker[Session] | None = None,
) -> None:
    """Download and import the extract covering a destination.

    **This opens its own Session, and that is not incidental.** When a route
    hands work to BackgroundTasks, the function runs *after* the response has
    been sent — by which point `get_db`'s `finally` has already closed the
    request's session. Using it here fails on first access with a closed-session
    error whose message looks nothing like its cause. A background job owns its
    session, start to finish.

    `session_factory` is the testing seam: the default is bound to the real
    database, so a test that triggered a sync would write to your actual rows.

    Never raises. A sync failing is a normal outcome, recorded on the
    destination for the UI to show; raising inside a BackgroundTask only
    produces a traceback nobody sees.
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

            # Remember the region, so a later change to the country table
            # cannot silently repoint an imported destination elsewhere.
            destination.region = region
            destination.sync_state = SyncState.SYNCING
            destination.sync_error = None
            db.commit()

            extract = extract_state(db, region)

            try:
                # --- download (skipped if the file is already on disk) ---
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
                        # Only remove our own flag; a later sync for the same
                        # region may have registered its own meanwhile.
                        if _downloads.get(region) is cancel:
                            del _downloads[region]
                extract.size_bytes = path.stat().st_size

                # --- import ---
                extract.state = ExtractState.IMPORTING
                db.commit()

                # The whole region, not a per-destination slice. osm_places is
                # global now, so there is nothing to scope it to — and this is
                # what makes every viewport inside the region instantly
                # available instead of only the city that triggered the import.
                written = import_extract(db, region)

                # Record where this region is, so the API can distinguish
                # "nothing here" from "never imported".
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

                # Everything in this region is now filled, not just the
                # destination that triggered the job.
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
                # Nothing needs this region any more (its last destination was
                # deleted). Remove the half-finished tracking row, so the
                # region looks never-started rather than stuck "downloading".
                db.rollback()
                unfinished = db.scalar(
                    select(OsmExtract).where(
                        OsmExtract.region == region,
                        OsmExtract.state != ExtractState.READY,
                    )
                )
                if unfinished is not None:
                    db.delete(unfinished)

                # Normally the destination is gone — that is why we cancelled.
                # If it is somehow still here, say so rather than leave it
                # spinning on "syncing".
                destination = _reload_destination(db, destination_id)
                if destination is not None:
                    destination.sync_state = SyncState.FAILED
                    destination.sync_error = "The download was cancelled."
                db.commit()
                logger.info("sync destination %s cancelled", destination_id)

            except ExtractError as exc:
                # A download or parse failure. The destination is still usable
                # for manual saves, and the error is retryable — a partial
                # download resumes rather than restarting.
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
        # In a finally, so a crash cannot leave the destination permanently
        # claimed and unsyncable until the process restarts.
        with _active_lock:
            _active.discard(destination_id)


def _reload_destination(db: Session, destination_id: int) -> Destination | None:
    """The destination as the database has it *now*, or None if deleted.

    Not `db.get()`: that returns the copy this session loaded at the start of
    the sync without asking the database. If the destination was deleted
    meanwhile — the normal case for a cancelled download — writing to that
    stale copy makes the commit fail with StaleDataError ("expected to update
    1 row; 0 were matched"), which also rolls back the cleanup around it.
    """
    return db.scalar(select(Destination).where(Destination.id == destination_id))


def get_sync_runner() -> Callable[[int], None]:
    """FastAPI dependency returning the function that starts a sync.

    Routes schedule `runner(destination_id)` on BackgroundTasks rather than
    calling sync_destination directly, so tests can swap in a recorder and
    assert "a sync was scheduled" without any download. The same seam as get_db.
    """
    return sync_destination
