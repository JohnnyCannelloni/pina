"""add place source and source_ref

    source      'manual' (typed in) or 'osm' (saved from the map)
    source_ref  the osm element id, e.g. "node/1234567". null for manual places.

lets the map stop suggesting places that are already saved.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # the table already has rows, so the not null column needs a server
    # default to backfill them. every earlier place was manual.
    op.add_column(
        "places",
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="manual",
        ),
    )

    # nullable, so existing rows just get null.
    op.add_column("places", sa.Column("source_ref", sa.String(), nullable=True))

    op.create_check_constraint(
        "ck_places_source",
        "places",
        "source IN ('manual', 'osm')",
    )

    # osm places need a ref, manual ones must not have one.
    op.create_check_constraint(
        "ck_places_source_ref_present",
        "places",
        "(source = 'osm') = (source_ref IS NOT NULL)",
    )

    # one save per osm place per destination (routes.py turns the violation
    # into a 409). no partial index needed: nulls don't collide.
    op.create_index(
        "ix_places_destination_source_ref",
        "places",
        ["destination_id", "source_ref"],
        unique=True,
    )


def downgrade() -> None:
    """reverse upgrade(): index and constraints first, then the columns."""
    op.drop_index("ix_places_destination_source_ref", table_name="places")
    op.drop_constraint("ck_places_source_ref_present", "places", type_="check")
    op.drop_constraint("ck_places_source", "places", type_="check")
    op.drop_column("places", "source_ref")
    op.drop_column("places", "source")
