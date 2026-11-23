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
    BigInteger,
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


class PlaceSource(StrEnum):
    """Where a saved place came from.

    MANUAL is a place you typed into the form. OSM is a suggestion you saved
    from the map, in which case Place.source_ref holds the OpenStreetMap element
    id so the map can tell it is already saved.

    Note what is *not* here: an LLM value. The LLM feature is known future work,
    but adding the value when the feature exists is a one-line change to this
    enum and to ck_places_source — whereas a value nothing can produce is a
    constraint that has stopped describing reality.
    """

    MANUAL = "manual"
    OSM = "osm"


class SyncState(StrEnum):
    """How a destination's OpenStreetMap place cache is doing.

    The map reads cached places from `osm_places`; this says whether that cache
    has been filled yet. It exists so the UI can be honest — a feature that
    depends on a flaky third party has to say what it is doing, and silence is
    what made the previous version of this feature feel broken.

    PENDING  created, never synced
    SYNCING  a background job is working through its tiles
    READY    every tile succeeded
    FAILED   at least one tile gave up; sync_error says why, and it is retryable
    """

    PENDING = "pending"
    SYNCING = "syncing"
    READY = "ready"
    FAILED = "failed"


class ExtractState(StrEnum):
    """How a region's OpenStreetMap extract is doing.

    Downloading and importing are separate states because they fail for
    different reasons and take very different amounts of time: a 250 MB
    download is minutes of network, the import is seconds of CPU. Collapsing
    them into one "working" state would leave the user unable to tell a slow
    download from a stuck parse.
    """

    PENDING = "pending"
    DOWNLOADING = "downloading"
    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"


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

    # The area to fetch places for, and the map's opening view. Added in 0003.
    # A centre alone cannot answer "what is in this city" — that needs an
    # extent. Set from the geocoder's bounding box, clamped by app/sync.py.
    south: Mapped[float] = mapped_column(Float, nullable=False)
    west: Mapped[float] = mapped_column(Float, nullable=False)
    north: Mapped[float] = mapped_column(Float, nullable=False)
    east: Mapped[float] = mapped_column(Float, nullable=False)

    # See SyncState. Stored as plain text with a CHECK constraint, same reasoning
    # as Place.category.
    sync_state: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending"
    )
    sync_error: Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The OSM id of the city itself, e.g. "relation/44915". Unique per trip, so
    # the same place cannot be added twice. NULL for the seeded destination,
    # which predates geocoding.
    osm_ref: Mapped[str | None] = mapped_column(String, nullable=True)

    # Added in 0004. Which country this is, and therefore which Geofabrik
    # extract covers it. Both nullable: a destination in a country we have no
    # region for is still usable, it just cannot auto-fill its places.
    country_code: Mapped[str | None] = mapped_column(String, nullable=True)
    region: Mapped[str | None] = mapped_column(String, index=True, nullable=True)

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

    # Added in migration 0002. See PlaceSource above.
    source: Mapped[str] = mapped_column(
        String, nullable=False, server_default="manual"
    )
    # The OpenStreetMap element id, e.g. "node/1234567". NULL for manual places.
    # Unique per destination, which is how the same suggestion cannot be saved
    # twice — see the index in 0002 and the 409 handling in routes.py.
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)

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
        # Added in 0002.
        CheckConstraint(
            "source IN ('manual', 'osm')",
            name="ck_places_source",
        ),
        # An OSM place must carry its reference; a manual one must not have one.
        CheckConstraint(
            "(source = 'osm') = (source_ref IS NOT NULL)",
            name="ck_places_source_ref_present",
        ),
        # Unique per destination. NULLs do not collide in a Postgres unique
        # index, so this constrains only real OSM references — see the longer
        # note in migration 0002.
        Index(
            "ix_places_destination_source_ref",
            "destination_id",
            "source_ref",
            unique=True,
        ),
    )


class OsmPlace(Base):
    """A place that exists in OpenStreetMap — a cache, not a record.

    The distinction from Place is the whole point:

        Place     you chose this. It is yours. It has must_go. Never deleted
                  behind your back.
        OsmPlace  the world happens to contain this. We cached it so the map
                  can show it. Deleting the whole table costs nothing but a
                  re-import.

    **Global since migration 0005.** There is no destination_id: one row per
    OpenStreetMap place, keyed on source_ref, shared by every destination and
    every client. Previously each destination held its own copy, which meant
    overlapping cities stored the same café twice and every new destination
    re-imported data the server already had. Reference data about the world is
    not per-user data.

    The practical consequence: importing a region once serves everyone, and no
    client ever downloads anything. The map just queries a rectangle.
    """

    __tablename__ = "osm_places"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: "node/1234567" — globally unique, and the upsert key, which is what makes
    #: a repeated import update rather than duplicate.
    source_ref: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    category: Mapped[str] = mapped_column(String, nullable=False)

    # NOT NULL, unlike Place.latitude: a cached OSM place without a position is
    # of no use to a map, which is the only thing this table feeds.
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)

    #: Which Geofabrik extract this came from, e.g. "us/new-york". Provenance,
    #: so a region can be refreshed or dropped wholesale.
    region: Mapped[str] = mapped_column(String, nullable=False, index=True)

    #: Added in 0006. The OSM tags worth keeping — cuisine, diet, outdoor
    #: seating, hours, website, phone — as chosen by `place_tags` in
    #: app/extract.py, which is the only writer. `cuisine` is a list
    #: (["italian", "pizza"]); everything else is the tag's own string value.
    #: NULL until the region is re-imported after 0006.
    # none_as_null: Python None is SQL NULL, not the JSON value `null` —
    # otherwise "no tags" would have two spellings and `IS NULL` would miss one.
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
        # The index behind every viewport query. latitude leads and Postgres
        # range-scans it, filtering longitude from the index rows. At very large
        # scale the answer is PostGIS with a GiST index on a geography column;
        # this project does not need that yet, and saying so is part of knowing
        # when it would.
        Index("ix_osm_places_coords", "latitude", "longitude"),
        # Serves the `tags @> ...` filters in app/ask.py. See migration 0006.
        Index(
            "ix_osm_places_tags",
            "tags",
            postgresql_using="gin",
            postgresql_ops={"tags": "jsonb_path_ops"},
        ),
    )


class OsmExtract(Base):
    """One downloaded regional OpenStreetMap file.

    This table exists to answer one question cheaply: *have we already got this
    country?* Adding Porto after Lisbon should cost no download, and without a
    record of what has been fetched there is no way to know.

    It replaced OsmTile, and the reason is worth keeping in mind. Tiles existed
    to make a per-request API resumable, because that API failed most of the
    time. A static file needs no work queue — you fetch it once and parse it.
    When a dependency changes, the data model built to cope with its weaknesses
    should go too, rather than lingering as unused tables.
    """

    __tablename__ = "osm_extracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Geofabrik path, e.g. "europe/malta". Unique — the whole point.
    region: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(
        String, nullable=False, server_default="pending"
    )
    #: Places this extract contributed, for telling the user something happened.
    place_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    #: BigInteger because a large extract exceeds the 2^31 an Integer holds.
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)

    # Where this region actually is, from the Geofabrik index. Added in 0005 so
    # the API can tell "nothing is here" apart from "this was never imported" —
    # an empty map with no explanation is what made two earlier versions of
    # this feature feel broken. Nullable because the row is created when a
    # download starts, before the bounds are necessarily known.
    south: Mapped[float | None] = mapped_column(Float, nullable=True)
    west: Mapped[float | None] = mapped_column(Float, nullable=True)
    north: Mapped[float | None] = mapped_column(Float, nullable=True)
    east: Mapped[float | None] = mapped_column(Float, nullable=True)

    imported_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'downloading', 'importing', 'ready', 'failed')",
            name="ck_osm_extracts_state",
        ),
        UniqueConstraint("region", name="uq_osm_extracts_region"),
    )
