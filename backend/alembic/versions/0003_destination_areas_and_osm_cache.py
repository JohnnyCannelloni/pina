"""destination areas, sync state, and the OpenStreetMap place cache

destinations gain a bounding box (added nullable, backfilled per row, then made
not null) and a sync state. two new tables:

    osm_places  cache of osm places per destination (made global in 0005)
    osm_tiles   work queue for the old overpass sync (dropped in 0004)

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

# backfill half-span in degrees (~6.5 km), matching app/sync.py at the time.
BACKFILL_HALF_SPAN = 0.06

BBOX_COLUMNS = ("south", "west", "north", "east")


def upgrade() -> None:
    # 1. add the box columns as nullable.
    for column in BBOX_COLUMNS:
        op.add_column(
            "destinations", sa.Column(column, sa.Float(), nullable=True)
        )

    # 2. backfill a box around each existing destination's centre.
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

    # 3. require them once no nulls remain.
    for column in BBOX_COLUMNS:
        op.alter_column("destinations", column, nullable=False)

    op.create_check_constraint(
        "ck_destinations_bbox_ordered",
        "destinations",
        "north > south AND east > west",
    )

    # every existing destination has never synced.
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
    op.add_column(
        "destinations", sa.Column("sync_error", sa.String(), nullable=True)
    )
    op.add_column(
        "destinations",
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
    )

    # osm id of the city, e.g. "relation/44915". null for the seed.
    op.add_column(
        "destinations", sa.Column("osm_ref", sa.String(), nullable=True)
    )
    # one city once per trip. nulls don't collide.
    op.create_index(
        "ix_destinations_trip_osm_ref",
        "destinations",
        ["trip_id", "osm_ref"],
        unique=True,
    )

    # --- osm_places --------------------------------------------------------
    op.create_table(
        "osm_places",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        # with destination_id, the upsert key.
        sa.Column("source_ref", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False),
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
    # viewport queries per destination.
    op.create_index(
        "ix_osm_places_destination_coords",
        "osm_places",
        ["destination_id", "latitude", "longitude"],
    )

    # --- osm_tiles -----------------------------------------------------------
    op.create_table(
        "osm_tiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        # south-west corner, snapped to a global grid.
        sa.Column("tile_south", sa.Float(), nullable=False),
        sa.Column("tile_west", sa.Float(), nullable=False),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="pending"
        ),
        # lets a retry give up on a tile that keeps failing.
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
        # one row per tile per destination; makes planning re-runnable.
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
    """reverse upgrade(), children first."""
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
