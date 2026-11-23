"""seed a starter trip (italy -> venice). run with `python -m app.seed`.

idempotent, so it's safe to run more than once. the tests call it too.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Destination, Trip

TRIP_NAME = "Italy"
DESTINATION_NAME = "Venice"

# map centre, roughly the rialto.
VENICE_LATITUDE = 45.4408
VENICE_LONGITUDE = 12.3155

# same half-span migration 0003 used for its backfill. hardcoded so seeding
# works offline; other destinations get their area from the geocoder.
VENICE_HALF_SPAN = 0.06


def seed(db: Session) -> Destination:
    """make sure italy -> venice exists and return venice. the caller commits."""
    trip = db.scalar(select(Trip).where(Trip.name == TRIP_NAME))
    if trip is None:
        trip = Trip(name=TRIP_NAME)
        db.add(trip)
        # flush to get trip.id without committing a half-seeded trip.
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
            south=VENICE_LATITUDE - VENICE_HALF_SPAN,
            north=VENICE_LATITUDE + VENICE_HALF_SPAN,
            west=VENICE_LONGITUDE - VENICE_HALF_SPAN,
            east=VENICE_LONGITUDE + VENICE_HALF_SPAN,
        )
        db.add(destination)
        db.flush()

    return destination


def main() -> None:
    with SessionLocal() as db:
        destination = seed(db)
        db.commit()
        print(
            f"Seeded: {TRIP_NAME} -> {DESTINATION_NAME} "
            f"(trip_id={destination.trip_id}, destination_id={destination.id})"
        )


if __name__ == "__main__":
    main()
