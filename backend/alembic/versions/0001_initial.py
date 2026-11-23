"""initial schema: trips, destinations, places

    trips -> destinations -> places   (fk, on delete cascade)

a place's latitude/longitude are optional: no pin means it only shows in the list.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- trips ------------------------------------------------------------
    op.create_table(
        "trips",
        # identity column; postgres assigns ids.
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            # timestamptz, so stored times are unambiguous.
            sa.DateTime(timezone=True),
            nullable=False,
            # database default, so rows inserted outside the orm get one too.
            server_default=sa.text("now()"),
        ),
    )

    # --- destinations -----------------------------------------------------
    op.create_table(
        "destinations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trip_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        # double precision (float64) round-trips through json exactly, so a pin
        # comes back in the same spot.
        sa.Column("latitude", sa.Float(), nullable=False),
        sa.Column("longitude", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["trip_id"],
            ["trips.id"],
            name="fk_destinations_trip_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_destinations_trip_id", "destinations", ["trip_id"]
    )

    # --- places -----------------------------------------------------------
    op.create_table(
        "places",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        # text plus a check constraint rather than a native enum: easier to
        # change later.
        sa.Column("category", sa.String(), nullable=False),
        sa.Column(
            "must_go",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        # nullable: a place may have no pin.
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["destination_id"],
            ["destinations.id"],
            name="fk_places_destination_id",
            ondelete="CASCADE",
        ),
        # constraints are named so errors are clear and downgrade can drop them.
        sa.CheckConstraint(
            "category IN ('restaurant', 'cafe', 'sight')",
            name="ck_places_category",
        ),
        # both coordinates or neither. PlaceCreate checks the same thing to
        # give a readable 422; this makes it true for every writer.
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name="ck_places_coords_both_or_neither",
        ),
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name="ck_places_lat_range",
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name="ck_places_lng_range",
        ),
    )
    # both place endpoints filter on destination_id.
    op.create_index("ix_places_destination_id", "places", ["destination_id"])


def downgrade() -> None:
    """drop everything, children first."""
    op.drop_index("ix_places_destination_id", table_name="places")
    op.drop_table("places")
    op.drop_index("ix_destinations_trip_id", table_name="destinations")
    op.drop_table("destinations")
    op.drop_table("trips")
