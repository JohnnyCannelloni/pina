"""replace the Overpass tile sync with regional OSM extracts

the public overpass api failed most requests, so places now come from
geofabrik's static regional files instead.

  - destinations record their country and region.
  - osm_extracts tracks each downloaded region, so a second city in the same
    region needs no download.
  - osm_tiles is dropped, and the old partial cache is cleared.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- destinations ------------------------------------------------------
    # nullable: a destination without a region is still usable by hand.
    op.add_column(
        "destinations", sa.Column("country_code", sa.String(), nullable=True)
    )
    # e.g. "portugal". stored so it can't be silently repointed later.
    op.add_column("destinations", sa.Column("region", sa.String(), nullable=True))
    op.create_index("ix_destinations_region", "destinations", ["region"])

    # --- osm_extracts ----------------------------------------------------------
    op.create_table(
        "osm_extracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        # e.g. "europe/malta". unique.
        sa.Column("region", sa.String(), nullable=False),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="pending"
        ),
        # places this extract contributed.
        sa.Column(
            "place_count", sa.Integer(), nullable=False, server_default="0"
        ),
        # large extracts exceed a 32-bit integer.
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'downloading', 'importing', 'ready', 'failed')",
            name="ck_osm_extracts_state",
        ),
        sa.UniqueConstraint("region", name="uq_osm_extracts_region"),
    )

    # --- drop the tile queue ---------------------------------------------------
    op.drop_index("ix_osm_tiles_destination_state", table_name="osm_tiles")
    op.drop_table("osm_tiles")

    # the cached places came from the half-working overpass sync; start clean.
    # saved places are untouched.
    op.execute(sa.text("DELETE FROM osm_places"))


def downgrade() -> None:
    """recreate an empty tile queue and drop the extract tracking."""
    op.create_table(
        "osm_tiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("tile_south", sa.Float(), nullable=False),
        sa.Column("tile_west", sa.Float(), nullable=False),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="pending"
        ),
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
            "state IN ('pending', 'done', 'failed')", name="ck_osm_tiles_state"
        ),
        sa.UniqueConstraint(
            "destination_id",
            "tile_south",
            "tile_west",
            name="uq_osm_tiles_destination_corner",
        ),
    )
    op.create_index(
        "ix_osm_tiles_destination_state", "osm_tiles", ["destination_id", "state"]
    )

    op.drop_table("osm_extracts")
    op.drop_index("ix_destinations_region", table_name="destinations")
    op.drop_column("destinations", "region")
    op.drop_column("destinations", "country_code")
