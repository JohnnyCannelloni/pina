"""fill places from the command line instead of a web request.

    python -m app.fill_places --list
    python -m app.fill_places --destination 1
    python -m app.fill_places --all

uses the same sync_destination as the api: one overpass query per destination,
skipped when another synced destination already covers its area.
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Destination, OsmPlace, SyncState
from app.sync import sync_destination


def _cached_count(db: Session, destination: Destination) -> int:
    """cached osm places inside a destination's area.

    osm_places is global, so this counts by area rather than by foreign key.
    """
    return db.scalar(
        select(func.count())
        .select_from(OsmPlace)
        .where(
            OsmPlace.latitude >= destination.south,
            OsmPlace.latitude <= destination.north,
            OsmPlace.longitude >= destination.west,
            OsmPlace.longitude <= destination.east,
        )
    ) or 0


def _list_destinations() -> None:
    """print every destination and how many cached places fall inside it."""
    with SessionLocal() as db:
        rows = list(db.scalars(select(Destination).order_by(Destination.id)))
        if not rows:
            print("No destinations. Run `python -m app.seed` first.")
            return

        print(f"{'id':>3}  {'name':<24} {'state':<9} {'places':>7}")
        for destination in rows:
            cached = _cached_count(db, destination)
            print(
                f"{destination.id:>3}  {destination.name[:24]:<24} "
                f"{destination.sync_state:<9} {cached:>7}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--destination", type=int, help="Destination id to fill.")
    group.add_argument("--all", action="store_true", help="Fill every destination.")
    group.add_argument(
        "--list", action="store_true", help="Show destinations and exit."
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Hide progress logging."
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list:
        _list_destinations()
        return

    if args.all:
        with SessionLocal() as db:
            ids = list(db.scalars(select(Destination.id).order_by(Destination.id)))
    else:
        ids = [args.destination]

    failures = 0
    for destination_id in ids:
        with SessionLocal() as db:
            destination = db.get(Destination, destination_id)
            if destination is None:
                print(f"destination {destination_id}: not found")
                failures += 1
                continue
            label = destination.name

        print(f"filling {label} (id {destination_id})…")
        sync_destination(destination_id)

        with SessionLocal() as db:
            destination = db.get(Destination, destination_id)
            # the destination may have been deleted while it synced.
            cached = _cached_count(db, destination) if destination else 0
            state = destination.sync_state if destination else "missing"
            error = destination.sync_error if destination else None

        if state == SyncState.READY:
            print(f"  done: {cached} places cached")
        else:
            print(f"  {state}: {error or 'see the log above'}")
            failures += 1

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
