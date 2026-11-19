"""destination areas, sync state, and the OpenStreetMap place cache

READ THIS FIRST. Two things happen here, and the first is the more instructive.

1. **Destinations gain an area and a sync state.** A destination used to be a
   name and a point. To fetch the places *in* a city you need its extent, so it
   gains south/west/north/east. Those are NOT NULL — and unlike `source` in
   0002, there is no sensible constant to default them to. A bounding box of
   (0,0,0,0) is a real place in the Gulf of Guinea, not a safe placeholder. So
   this migration uses the **three-step pattern** instead: add the column
   nullable, backfill it with a real value computed per row, then make it
   required. That sequence is the standard answer whenever a required column has
   no universal default, and it is the main thing to take from this file.

2. **Two new tables for place discovery**, and the point is what is *not* here:
   neither of them touches `places`. Keeping them separate is what makes
   re-syncing a city safe.

       places      YOUR collection. Curated, has must_go. You own it.
       osm_places  a CACHE of what exists in the world, per destination.
                   Wipeable and refetchable; losing it costs you nothing.
       osm_tiles   the WORK QUEUE. One row per square of a destination, so a
                   sync that dies halfway can resume instead of restarting.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Half-width, in degrees, of the box used to backfill destinations that predate
# this migration. 0.06 degrees of latitude is about 6.5 km, so the box spans
# roughly 13 km — the same clamp app/sync.py applies to new destinations, which
# keeps old and new rows consistent.
BACKFILL_HALF_SPAN = 0.06

BBOX_COLUMNS = ("south", "west", "north", "east")


def upgrade() -> None:
    # --- step 1: add the bbox columns NULLABLE ---------------------------
    #
    # Nullable, so the ALTER succeeds instantly against existing rows. If these
    # were declared NOT NULL here, Postgres would reject the statement outright:
    #     column "south" of relation "destinations" contains null values
    for column in BBOX_COLUMNS:
        op.add_column(
            "destinations", sa.Column(column, sa.Float(), nullable=True)
        )

    # --- step 2: backfill every existing row ----------------------------
    #
    # A plain UPDATE in the migration. This is the part that cannot be expressed
    # as a server_default: the value depends on the row, since each destination
    # gets a box around *its own* centre.
    #
    # op.execute runs raw SQL in the migration's transaction, so if anything
    # below fails the whole migration rolls back and the columns do not linger
    # half-populated. (Postgres allows DDL in a transaction — see the note in
    # alembic/env.py.)
    op.execute(
        sa.text(
            """
            UPDATE destinations
               SET south = latitude  - :half,
                   north = latitude  + :half,
                   west  = longitude - :half,
                   east  = longitude + :half
             WHERE south IS NULL
            """
        ).bindparams(half=BACKFILL_HALF_SPAN)
    )

    # --- step 3: now that no NULLs remain, require the columns -----------
    #
    # Only safe because step 2 ran. Swap the order of steps 2 and 3 and this
    # migration fails on any database that already has a destination in it —
    # which is to say, on the real one but not on an empty test database. That
    # asymmetry is why this pattern is worth learning rather than rediscovering
    # during a deploy.
    for column in BBOX_COLUMNS:
        op.alter_column("destinations", column, nullable=False)

    op.create_check_constraint(
        "ck_destinations_bbox_ordered",
        "destinations",
        "north > south AND east > west",
    )

    # --- sync state ------------------------------------------------------
    # This one DOES have a sensible default, so it is a one-liner: every
    # existing destination has never been synced, which is exactly 'pending'.
    op.add_column(
        "destinations",
        sa.Column(
            "sync_state",
            sa.String(),
            nullable=False,
            server_default="pending",
        ),
    )
    op.create_check_constraint(
        "ck_destinations_sync_state",
        "destinations",
        "sync_state IN ('pending', 'syncing', 'ready', 'failed')",
    )
    # Nullable: there is no error until there is an error.
    op.add_column(
        "destinations", sa.Column("sync_error", sa.String(), nullable=True)
    )
    op.add_column(
        "destinations",
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
    )

    # The OpenStreetMap id of the place itself, e.g. "relation/44915" for
    # Venice. Nullable because the seeded destination predates geocoding.
    op.add_column(
        "destinations", sa.Column("osm_ref", sa.String(), nullable=True)
    )
    # Stops the same city being added to one trip twice. As in 0002, NULLs do
    # not collide in a Postgres unique index, so hand-made destinations with no
    # osm_ref are unaffected.
    op.create_index(
        "ix_destinations_trip_osm_ref",
        "destinations",
        ["trip_id", "osm_ref"],
        unique=True,
    )

    # --- osm_places: the cache of what exists in the world ---------------
    op.create_table(
        "osm_places",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        # "node/1234567". Together with destination_id this is the upsert key,
        # which is what makes re-syncing idempotent instead of duplicating.
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
        # NOT NULL here, unlike places.latitude: a cached OSM place always has a
        # position — that is the only reason it is useful to us.
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["destinations.id"],
            name="fk_osm_places_destination_id",
            # Delete a destination and its cache goes with it. No orphan rows
            # for a city you are no longer planning.
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "category IN ('restaurant', 'cafe', 'sight')",
            name="ck_osm_places_category",
        ),
        sa.UniqueConstraint(
            "destination_id", "source_ref", name="uq_osm_places_destination_ref"
        ),
    )
    # The index that makes the map fast. Every read is "places for destination N
    # within this rectangle", so destination_id leads and the coordinates
    # follow. This is the whole reason /nearby can answer in milliseconds
    # instead of calling Overpass.
    op.create_index(
        "ix_osm_places_destination_coords",
        "osm_places",
        ["destination_id", "latitude", "longitude"],
    )

    # --- osm_tiles: the work queue ---------------------------------------
    op.create_table(
        "osm_tiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        # South-west corner of the tile, snapped to a fixed global grid (see
        # TILE_SIZE_DEGREES in app/sync.py). Storing the corner rather than an
        # index keeps the rows meaningful on their own in psql.
        sa.Column("tile_south", sa.Float(), nullable=False),
        sa.Column("tile_west", sa.Float(), nullable=False),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="pending"
        ),
        # Lets a retry give up on a tile that keeps failing, instead of looping
        # on it forever while the rest of the city stays unsynced.
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["destinations.id"],
            name="fk_osm_tiles_destination_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'done', 'failed')",
            name="ck_osm_tiles_state",
        ),
        # One row per tile per destination. This is what makes planning tiles
        # safe to re-run: the insert is an upsert on this key.
        sa.UniqueConstraint(
            "destination_id",
            "tile_south",
            "tile_west",
            name="uq_osm_tiles_destination_corner",
        ),
    )
    op.create_index(
        "ix_osm_tiles_destination_state",
        "osm_tiles",
        ["destination_id", "state"],
    )


def downgrade() -> None:
    """Reverse upgrade(). Children before parents, newest change first.

    The bbox columns just get dropped — there is nothing to un-backfill, since
    the data they held did not exist before this migration.
    """
    op.drop_index("ix_osm_tiles_destination_state", table_name="osm_tiles")
    op.drop_table("osm_tiles")

    op.drop_index(
        "ix_osm_places_destination_coords", table_name="osm_places"
    )
    op.drop_table("osm_places")

    op.drop_index("ix_destinations_trip_osm_ref", table_name="destinations")
    op.drop_column("destinations", "osm_ref")
    op.drop_column("destinations", "synced_at")
    op.drop_column("destinations", "sync_error")
    op.drop_constraint(
        "ck_destinations_sync_state", "destinations", type_="check"
    )
    op.drop_column("destinations", "sync_state")

    op.drop_constraint(
        "ck_destinations_bbox_ordered", "destinations", type_="check"
    )
    for column in BBOX_COLUMNS:
        op.drop_column("destinations", column)
