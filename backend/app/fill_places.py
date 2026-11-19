"""CLI for filling a destination's places outside a web request.

    python -m app.fill_places --destination 1
    python -m app.fill_places --all
    python -m app.fill_places --list

Why this exists alongside the automatic background fill: the first destination
in a country needs its whole national extract, which for somewhere like Italy
is well over a gigabyte. That is a fine thing to run from a terminal where you
can watch it and nothing times out, and a poor thing to start from a web
request. Later cities in the same country are filled in seconds either way.

It calls the same `sync_destination` the API does — not a parallel
implementation — so anything proved here is true of the automatic path.
"""

from __future__ import annotations

import argparse
import logging

from sqlalchemy import func, select

from app.db import SessionLocal
from app.extract import download_extract, extract_state, import_extract
from app.geocode import get_geocoder
from app.models import (
    Destination,
    ExtractState,
    OsmExtract,
    OsmPlace,
    SyncState,
)
from app.regions import candidates_for, find_region
from app.sync import sync_destination


def _list_destinations() -> None:
    """Show every destination and how full its place cache is."""
    with SessionLocal() as db:
        rows = list(db.scalars(select(Destination).order_by(Destination.id)))
        if not rows:
            print("No destinations. Run `python -m app.seed` first.")
            return

        print(f"{'id':>3}  {'name':<24} {'state':<9} {'places':>7}  region")
        for destination in rows:
            # Counted by the destination's own area, not by a foreign key:
            # osm_places is global since 0005, so "how many places does this
            # destination have" is a geographic question now.
            cached = db.scalar(
                select(func.count())
                .select_from(OsmPlace)
                .where(
                    OsmPlace.latitude >= destination.south,
                    OsmPlace.latitude <= destination.north,
                    OsmPlace.longitude >= destination.west,
                    OsmPlace.longitude <= destination.east,
                )
            ) or 0
            print(
                f"{destination.id:>3}  {destination.name[:24]:<24} "
                f"{destination.sync_state:<9} {cached:>7}  "
                f"{destination.region or '(no map data)'}"
            )


def _list_regions_near(query: str) -> None:
    """Show which Geofabrik regions cover a place, smallest first.

    Prints the alternatives as well as the winner, because that is how you would
    spot a bad resolution. A bounding-box version of this lookup put New York
    City in New Jersey and Lisbon in Spain; seeing the runners-up is what makes
    such a mistake obvious instead of invisible.
    """
    results = get_geocoder().search(query)
    if not results:
        print(f"No place found for {query!r}.")
        return

    place = results[0]
    print(f"{place.display_name}")
    print(f"  {place.latitude:.4f}, {place.longitude:.4f}")
    print()

    regions = candidates_for(place.latitude, place.longitude)
    if not regions:
        print("  No OpenStreetMap region covers this point.")
        return

    print(f"  {'region':<28} {'bbox area':>10}")
    for index, region in enumerate(regions):
        marker = "->" if index == 0 else "  "
        print(f"  {marker} {region.id:<25} {region.area:>10.1f}")
    print()
    print(f"  Pina would download: {regions[0].id}")


def _import_region(region_id: str) -> bool:
    """Download and import one region. The operator entry point.

    Separate from --destination because on a hosted deployment this is the only
    way data gets in: clients never trigger a download. See AUTO_IMPORT_REGIONS
    in app/config.py.
    """
    try:
        region = find_region(region_id)
    except Exception as exc:
        print(f"  {exc}")
        return False

    print(f"  resolved {region.id} -> {region.pbf_url}")

    with SessionLocal() as db:
        extract = extract_state(db, region.id)
        try:
            extract.state = ExtractState.DOWNLOADING
            db.commit()
            path = download_extract(region.id)

            extract.state = ExtractState.IMPORTING
            extract.size_bytes = path.stat().st_size
            db.commit()

            written = import_extract(db, region.id)

            extract.state = ExtractState.READY
            extract.place_count = written
            extract.south, extract.west = region.south, region.west
            extract.north, extract.east = region.north, region.east
            db.commit()
            print(f"  imported {written} places from {region.id}")
            return True
        except Exception as exc:
            db.rollback()
            extract = extract_state(db, region.id)
            extract.state = ExtractState.FAILED
            extract.error = str(exc)
            db.commit()
            print(f"  failed: {exc}")
            return False


def _warn_about_size(destination_id: int) -> None:
    """Tell the user up front if this will be a large download.

    A 1.7 GB download that starts with no warning looks like a hang. Saying so
    first is the difference between waiting and killing the process.
    """
    with SessionLocal() as db:
        destination = db.get(Destination, destination_id)
        if destination is None or not destination.region:
            return

        already = db.scalar(
            select(OsmExtract).where(OsmExtract.region == destination.region)
        )
        if already is not None and already.state == "ready":
            print(f"  {destination.region} is already imported — this will be quick.")
            return

        print(
            f"  {destination.region} has not been downloaded yet. This runs "
            f"once per region; later cities inside it are instant."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--destination", type=int, help="Destination id to fill.")
    group.add_argument("--all", action="store_true", help="Fill every destination.")
    group.add_argument(
        "--list", action="store_true", help="Show destinations and exit."
    )
    group.add_argument(
        "--region", help="Download and import one Geofabrik region id."
    )
    group.add_argument(
        "--list-regions",
        action="store_true",
        help="Show which regions cover a place (use with --near).",
    )
    parser.add_argument(
        "--near", help="Place name for --list-regions, e.g. 'New York City'."
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Hide download progress logging."
    )
    args = parser.parse_args()

    # Logging on by default: this command's whole value is that you can watch a
    # long download, so hiding progress would defeat the point.
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.list:
        _list_destinations()
        return

    if args.list_regions:
        if not args.near:
            parser.error("--list-regions requires --near 'Some City'")
        _list_regions_near(args.near)
        return

    if args.region:
        if not _import_region(args.region):
            raise SystemExit(1)
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
        _warn_about_size(destination_id)
        sync_destination(destination_id)

        with SessionLocal() as db:
            destination = db.get(Destination, destination_id)
            cached = db.scalar(
                select(func.count())
                .select_from(OsmPlace)
                .where(OsmPlace.destination_id == destination_id)
            ) or 0
            state = destination.sync_state if destination else "missing"
            error = destination.sync_error if destination else None

        if state == SyncState.READY:
            print(f"  done: {cached} places cached")
        else:
            print(f"  {state}: {error or 'see the log above'}")
            failures += 1

    # Non-zero exit on failure, so this is usable in a script and the outcome is
    # unambiguous rather than buried in the output.
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
