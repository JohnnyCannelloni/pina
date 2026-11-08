"""The HTTP endpoints. Three of them.

This is the middle of the trace. Follow a save from here:

    AddPlaceForm.handleSubmit (React)
      -> POST /api/destinations/1/places
      -> create_place (below)
      -> PlaceCreate validates the body          (schemas.py)
      -> Place(...) builds the row               (models.py)
      -> db.add / db.commit issues the INSERT    (db.py holds the Session)
      -> PlaceRead serialises the saved row back (schemas.py)
      -> React prepends it to state
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Destination, Place, Trip
from app.schemas import PlaceCreate, PlaceRead, TripRead

router = APIRouter(prefix="/api")


@router.get("/trips", response_model=list[TripRead])
def list_trips(db: Session = Depends(get_db)) -> list[Trip]:
    """Every trip, with its destinations nested.

    The UI calls this on load to discover that Venice is destination 1 and where
    to centre the map, so no id or coordinate is hardcoded in the frontend.

    selectinload tells SQLAlchemy to fetch all the destinations in one extra
    query. Without it, serialising N trips would lazily fire one query per trip
    — the N+1 problem. With two rows seeded you would never notice; it is here
    because this is the shape of the bug, not because it hurts yet.
    """
    stmt = select(Trip).options(selectinload(Trip.destinations)).order_by(Trip.id)
    return list(db.scalars(stmt))


def _get_destination_or_404(db: Session, destination_id: int) -> Destination:
    """Look up a destination, or raise a clean 404.

    Both place endpoints need this. Without the explicit check, a POST to a
    destination that does not exist would reach Postgres and fail the foreign
    key, surfacing as a 500 and an ugly traceback. A 404 with a readable
    `detail` is the honest answer: the client asked about something absent.
    """
    destination = db.get(Destination, destination_id)
    if destination is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Destination {destination_id} not found",
        )
    return destination


@router.get("/destinations/{destination_id}/places", response_model=list[PlaceRead])
def list_places(
    destination_id: int, db: Session = Depends(get_db)
) -> list[Place]:
    """The destination's saved places — the one read both views share.

    The list renders all of these. The map renders the subset with coordinates.
    Because there is a single source, switching views cannot show stale data and
    a refresh cannot lose a pin.
    """
    _get_destination_or_404(db, destination_id)

    stmt = (
        select(Place)
        .where(Place.destination_id == destination_id)
        # Must-go first, then newest. desc() on a boolean puts true before
        # false in Postgres, which is what the flag is for.
        .order_by(Place.must_go.desc(), Place.created_at.desc(), Place.id.desc())
    )
    return list(db.scalars(stmt))


@router.post(
    "/destinations/{destination_id}/places",
    response_model=PlaceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_place(
    destination_id: int,
    payload: PlaceCreate,
    db: Session = Depends(get_db),
) -> Place:
    """Save one place. This is the endpoint the Save button hits.

    By the time the body of this function runs, FastAPI has already:
      - parsed the JSON body into a PlaceCreate,
      - rejected a blank name, an unknown category, an out-of-range coordinate,
        or a half-dropped pin, with a 422 and a field-level message.

    So there is no validation code here. That is the point of the schema.
    """
    destination = _get_destination_or_404(db, destination_id)

    # Build the row. destination_id comes from the URL, never the body, so the
    # place cannot be filed under a different destination than the one the
    # client addressed.
    place = Place(
        destination_id=destination.id,
        name=payload.name,
        # payload.category is a Category (a StrEnum). str() stores the plain
        # "restaurant" that ck_places_category expects, rather than relying on
        # how the enum happens to serialise.
        category=str(payload.category),
        must_go=payload.must_go,
        latitude=payload.latitude,
        longitude=payload.longitude,
    )

    # Three steps, and they are genuinely different:
    #   add    — stage the object in the Session's unit of work. No SQL yet.
    #   commit — flush the INSERT and commit the transaction. The row is now
    #            durable; this is the moment the save actually happens.
    #   refresh— re-SELECT the row to pick up what the database generated:
    #            `id` from the sequence and `created_at` from DEFAULT now().
    #            Without it those attributes would be None in the response.
    db.add(place)
    db.commit()
    db.refresh(place)

    # Returning the saved row (rather than 201 with an empty body) lets React
    # drop the real record — with its real id — straight into state, so the new
    # place and its pin appear without a second round trip.
    return place
