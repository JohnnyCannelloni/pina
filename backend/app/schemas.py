"""Pydantic schemas — the shape of the JSON going in and coming out.

Why these exist when `models.py` already describes a place: the ORM model is the
*storage* shape and the schema is the *wire* shape, and they differ. A client
may not set `id` or `created_at`; the database must never be handed a `category`
it has not agreed to. Separating them means a request body can be validated and
rejected with a 422 before any SQL runs.

Two separate classes per direction is the convention worth internalising:
  PlaceCreate — what a client may send   (no id, no created_at)
  PlaceRead   — what the API sends back  (everything)
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import Category

# Reused by PlaceCreate. strip_whitespace runs *before* min_length, so a name of
# "   " becomes "" and is then rejected — which is the behaviour you want, and
# is easy to get wrong by validating length first.
PlaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]

# ge/le = "greater or equal"/"less or equal". These mirror the ck_places_*_range
# constraints in the migration. The schema check exists to produce a readable
# 422; the database check exists to actually be true.
Latitude = Annotated[float, Field(ge=-90, le=90)]
Longitude = Annotated[float, Field(ge=-180, le=180)]


class PlaceCreate(BaseModel):
    """The body of POST /api/destinations/{id}/places.

    Note what is absent: `id`, `created_at`, and `destination_id`. The first two
    belong to the database, and the third comes from the URL path — accepting it
    in the body as well would let a client send a path and body that disagree.
    """

    name: PlaceName
    category: Category
    must_go: bool = False

    # Optional: a place with no pin is normal. Default None rather than required
    # means an older client that knows nothing about the map still works.
    latitude: Latitude | None = None
    longitude: Longitude | None = None

    @model_validator(mode="after")
    def coordinates_are_both_or_neither(self) -> Self:
        """Reject a half-dropped pin.

        A field_validator cannot do this — it only sees one field. A
        model_validator with mode="after" runs once both fields are parsed, so
        it can compare them.

        The same rule is enforced by ck_places_coords_both_or_neither in the
        database. That is deliberate duplication, not an oversight: this copy
        exists to return a 422 that says what is wrong, while the database copy
        is the one that cannot be bypassed by a future endpoint or a stray psql
        INSERT. Validation is for error messages; constraints are for truth.
        """
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError(
                "latitude and longitude must be provided together, or both omitted"
            )
        return self


class PlaceRead(BaseModel):
    """A saved place as the API returns it."""

    # from_attributes lets Pydantic read a SQLAlchemy object's attributes
    # directly (place.name), instead of requiring a dict. Without it,
    # PlaceRead.model_validate(place) raises.
    model_config = ConfigDict(from_attributes=True)

    id: int
    destination_id: int
    name: str
    category: Category
    must_go: bool
    latitude: float | None
    longitude: float | None
    created_at: datetime


class DestinationRead(BaseModel):
    """A destination, including the coordinates the map centres on."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    latitude: float
    longitude: float


class TripRead(BaseModel):
    """A trip with its destinations nested.

    Nesting means the UI finds Venice's id and map centre in one request, so no
    destination id is ever hardcoded in the React code.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    destinations: list[DestinationRead]
