"""Seed the one trip and one destination this version of Pina works with.

Run it after migrating:
    python -m app.seed

Why this is a script and not part of the migration: migrations describe the
*schema*, and mixing rows into them makes them non-rerunnable (the second
`alembic upgrade head` on a fresh database would try to insert Venice twice) and
quietly ties your test fixtures to schema history. Keeping seeds separate means
you can reseed without migrating and migrate without reseeding.

It is **idempotent** — look-then-insert, so running it ten times still leaves
one Italy and one Venice. That matters because the test suite calls seed() for
every test.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Destination, Trip

TRIP_NAME = "Italy"
DESTINATION_NAME = "Venice"

# Where the map opens. Roughly the Rialto, which keeps the whole of the historic
# centre in view at the zoom the UI uses.
VENICE_LATITUDE = 45.4408
VENICE_LONGITUDE = 12.3155


def seed(db: Session) -> Destination:
    """Ensure Italy -> Venice exists; return the Venice destination.

    Takes a Session rather than making its own, so the tests can run it inside
    their own transaction and roll it back. A function that creates its own
    session is a function you cannot reuse.
    """
    trip = db.scalar(select(Trip).where(Trip.name == TRIP_NAME))
    if trip is None:
        trip = Trip(name=TRIP_NAME)
        db.add(trip)
        # flush sends the INSERT so the database assigns trip.id, without
        # committing. We need that id for the destination's foreign key below.
        # (commit would also work, but then a failure halfway through would
        # leave a trip with no destination.)
        db.flush()

    destination = db.scalar(
        select(Destination).where(
            Destination.trip_id == trip.id,
            Destination.name == DESTINATION_NAME,
        )
    )
    if destination is None:
        destination = Destination(
            trip_id=trip.id,
            name=DESTINATION_NAME,
            latitude=VENICE_LATITUDE,
            longitude=VENICE_LONGITUDE,
        )
        db.add(destination)
        db.flush()

    return destination


def main() -> None:
    with SessionLocal() as db:
        destination = seed(db)
        # The caller commits, not seed() — same reason seed() takes a Session.
        db.commit()
        print(
            f"Seeded: {TRIP_NAME} -> {DESTINATION_NAME} "
            f"(trip_id={destination.trip_id}, destination_id={destination.id})"
        )


if __name__ == "__main__":
    main()
