"""sqlalchemy models. the migrations in alembic/versions create the tables.

    Trip (italy)
      └── Destination (venice)            lat/lng = map centre
            └── Place (trattoria rosa)    optional lat/lng = the pin
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
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Category(StrEnum):
    """place categories. also enforced by the ck_*_category constraints."""

    RESTAURANT = "restaurant"
    CAFE = "cafe"
    SIGHT = "sight"


class PlaceSource(StrEnum):
    """where a saved place came from. OSM places carry their osm id in source_ref."""

    MANUAL = "manual"
    OSM = "osm"


class SyncState(StrEnum):
    """state of a destination's place cache.

    PENDING  created, never synced
    SYNCING  a background job is running
    READY    imported
    FAILED   sync_error says why; retryable
    """

    PENDING = "pending"
    SYNCING = "syncing"
    READY = "ready"
    FAILED = "failed"


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # server_default so rows inserted outside the orm get a timestamp too.

    destinations: Mapped[list[Destination]] = relationship(
        back_populates="trip",
        cascade="all, delete-orphan",
        # stable order across refreshes.
        order_by="Destination.id",
    )


class Destination(Base):
    __tablename__ = "destinations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trip_id: Mapped[int] = mapped_column(
        # db-level cascade; the relationship cascade covers loaded objects.
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # map centre.
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    # map area, from the geocoder's box (clamped in app/sync.py).
    south: Mapped[float] = mapped_column(Float, nullable=False)
    west: Mapped[float] = mapped_column(Float, nullable=False)
    north: Mapped[float] = mapped_column(Float, nullable=False)
    east: Mapped[float] = mapped_column(Float, nullable=False)

    # see SyncState.
    sync_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending"
    )
    sync_error: Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # osm id of the city, e.g. "relation/44915". unique per trip. null for the seed.
    osm_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    trip: Mapped[Trip] = relationship(back_populates="destinations")
    places: Mapped[list[Place]] = relationship(
        back_populates="destination", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "north > south AND east > west", name="ck_destinations_bbox_ordered"
        ),
        CheckConstraint(
            "sync_state IN ('pending', 'syncing', 'ready', 'failed')",
            name="ck_destinations_sync_state",
        ),
        Index(
            "ix_destinations_trip_osm_ref", "trip_id", "osm_ref", unique=True
        ),
    )


class Place(Base):
    """a saved restaurant, café or sight, with an optional pin.

    the list and the map both read this table.
    """

    __tablename__ = "places"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("destinations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # plain text plus a check constraint, not a native enum.
    category: Mapped[str] = mapped_column(String, nullable=False)

    must_go: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    # null when the place has no pin.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)

    # see PlaceSource.
    source: Mapped[str] = mapped_column(
        String, nullable=False, server_default="manual"
    )
    # osm id, e.g. "node/1234567". null for manual places. unique per destination.
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    destination: Mapped[Destination] = relationship(back_populates="places")

    # mirrors the constraints created by the migrations.
    __table_args__ = (
        CheckConstraint(
            "category IN ('restaurant', 'cafe', 'sight')",
            name="ck_places_category",
        ),
        # a pin has both coordinates or neither.
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
        CheckConstraint(
            "source IN ('manual', 'osm')",
            name="ck_places_source",
        ),
        # osm places need a ref; manual places must not have one.
        CheckConstraint(
            "(source = 'osm') = (source_ref IS NOT NULL)",
            name="ck_places_source_ref_present",
        ),
        # nulls don't collide in a unique index, so this only covers osm refs.
        Index(
            "ix_places_destination_source_ref",
            "destination_id",
            "source_ref",
            unique=True,
        ),
    )


class OsmPlace(Base):
    """a place that exists in openstreetmap. a cache, not user data.

    global since migration 0005: one row per osm place, shared by every
    destination, filled by app/overpass.py. safe to truncate and re-sync.
    """

    __tablename__ = "osm_places"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: "<type>/<id>", e.g. "node/1234567" or "way/42". globally unique; the
    #: upsert key, and what Place.source_ref links to.
    source_ref: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)

    # always set: this table only feeds the map.
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    #: whitelisted osm tags written by `place_tags` in app/overpass.py.
    #: `cuisine` is a list; other values are osm strings. none_as_null so None
    #: is sql null, not json null.
    tags: Mapped[dict[str, object] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "category IN ('restaurant', 'cafe', 'sight')",
            name="ck_osm_places_category",
        ),
        UniqueConstraint("source_ref", name="uq_osm_places_source_ref"),
        # viewport queries. postgis would be the next step at a much larger scale.
        Index("ix_osm_places_coords", "latitude", "longitude"),
        # tag filters in app/ask.py.
        Index(
            "ix_osm_places_tags",
            "tags",
            postgresql_using="gin",
            postgresql_ops={"tags": "jsonb_path_ops"},
        ),
    )

