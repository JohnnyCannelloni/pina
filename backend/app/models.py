"""SQLAlchemy models — the Python mirror of the tables.

Read `alembic/versions/0001_initial.py` first: *that* file is what actually
creates the tables. These classes describe the same shape so Python can query
it. Nothing here changes your database on its own — if you add a column below
and forget the migration, you get a runtime error about a missing column, not a
new column. Keeping those two in step by hand is the deal Alembic offers.

Shape of the data:

    Trip (Italy)
      └── Destination (Venice)            has its own lat/lng = the map centre
            └── Place (Trattoria Rosa)    optional lat/lng = the dropped pin
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Category(StrEnum):
    """The categories a place can have.

    StrEnum (not plain Enum) means `Category.CAFE == "cafe"` is True, so these
    values pass straight into SQL and JSON without a conversion step.

    This enum is one of only two places the category list is written down; the
    other is the ck_places_category constraint in the migration. Adding "bar"
    later means editing both — Python rejects it with a clean 422, Postgres
    rejects it no matter what code does.
    """

    RESTAURANT = "restaurant"
    CAFE = "cafe"
    SIGHT = "sight"


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # server_default (above) vs default: server_default puts DEFAULT now() in the
    # table definition, so Postgres fills the value. A Python-side `default`
    # would fill it in the app, meaning rows inserted by psql or a seed script
    # bypassing the ORM would have no timestamp. Let the database own it.

    destinations: Mapped[list[Destination]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
        # Without an explicit order, Postgres may return rows in any order it
        # likes. Sorting by id keeps the UI stable across refreshes.
        order_by="Destination.id",
    )


class Destination(Base):
    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(
        # ondelete="CASCADE" is a *database* rule: delete a trip and Postgres
        # deletes its destinations. The cascade= on the relationship above is a
        # separate *Python* rule for objects already loaded in a session. You
        # generally want both; they cover different paths to the same deletion.
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # NOT NULL: every destination needs a map centre. Venice's coordinates are a
    # fact about Venice, so they belong in the row — not as a constant in React.
    # That is what makes adding a second city a seed change, not a code change.
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    trip: Mapped[Trip] = relationship(back_populates="destinations")
    places: Mapped[list[Place]] = relationship(
        back_populates="destination", cascade="all, delete-orphan"
    )


class Place(Base):
    """A saved restaurant, café, or sight — with an optional pin.

    One table feeds both the list and the map. The map is not a separate store;
    it is this same set of rows filtered to those with coordinates. That is why
    "save a place, switch to Map, refresh, find the pin in the same spot" needs
    no synchronising to be true.
    """

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # Stored as plain text, validated by the CHECK constraint below. See the
    # note in Category, and 0001_initial.py, for why this is not a Postgres
    # native ENUM type.
    category: Mapped[str] = mapped_column(String, nullable=False)

    must_go: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    # Nullable, because a place saved without a pin is a normal, supported thing
    # — it just does not appear on the map. Float here is Postgres DOUBLE
    # PRECISION (float64), which round-trips through JSON exactly; that is what
    # lets the test assert the pin comes back at precisely the same spot.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    destination: Mapped[Destination] = relationship(back_populates="places")

    # These mirror the constraints in the migration. Declaring them here does
    # not create them — the migration does. They are repeated so that reading
    # this model tells you the real rules.
    __table_args__ = (
        CheckConstraint(
            "category IN ('restaurant', 'cafe', 'sight')",
            name="ck_places_category",
        ),
        # A place has a pin or it has not. One coordinate without the other is
        # meaningless, and making that the database's rule means no future
        # endpoint, script, or psql session has to remember it.
        CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name="ck_places_coords_both_or_neither",
        ),
        CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name="ck_places_lat_range",
        ),
        CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name="ck_places_lng_range",
        ),
    )
