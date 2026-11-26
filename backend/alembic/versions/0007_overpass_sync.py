"""load places per destination from overpass; drop geofabrik region tracking

places now come from one overpass query per destination box (app/overpass.py)
instead of whole geofabrik regions, so the region bookkeeping goes:

  - osm_extracts is dropped. coverage is now judged from synced destinations.
  - destinations.region and destinations.country_code are dropped.
  - osm_places.region (provenance by extract) is dropped. cached places stay.

Revision ID: 0007
Revises: 0006
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("osm_extracts")

    op.drop_index("ix_destinations_region", table_name="destinations")
    op.drop_column("destinations", "region")
    op.drop_column("destinations", "country_code")

    op.drop_index("ix_osm_places_region", table_name="osm_places")
    op.drop_column("osm_places", "region")


def downgrade() -> None:
    """put the columns and table back, empty.

    the dropped region values can't be recovered: cached places get 'unknown'
    and destinations get null, so an old importer would treat them as unresolved.
    """
    op.add_column("osm_places", sa.Column("region", sa.String(), nullable=True))
    op.execute(sa.text("UPDATE osm_places SET region = 'unknown'"))
    op.alter_column("osm_places", "region", nullable=False)
    op.create_index("ix_osm_places_region", "osm_places", ["region"])

    op.add_column(
        "destinations", sa.Column("country_code", sa.String(), nullable=True)
    )
    op.add_column("destinations", sa.Column("region", sa.String(), nullable=True))
    op.create_index("ix_destinations_region", "destinations", ["region"])

    # as 0004 created it, plus the bounds 0005 added.
    op.create_table(
        "osm_extracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("region", sa.String(), nullable=False),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="pending"
        ),
        sa.Column(
            "place_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("south", sa.Float(), nullable=True),
        sa.Column("west", sa.Float(), nullable=True),
        sa.Column("north", sa.Float(), nullable=True),
        sa.Column("east", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'downloading', 'importing', 'ready', 'failed')",
            name="ck_osm_extracts_state",
        ),
        sa.UniqueConstraint("region", name="uq_osm_extracts_region"),
    )
