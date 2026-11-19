"""add place source and source_ref

READ THIS FIRST. It is the more instructive of the two migrations, because
unlike 0001 it runs against a table that **already has rows in it**. That single
fact drives every decision below, and it is the situation nearly every migration
you ever write will be in.

Why these columns exist: the map can now suggest real places from
OpenStreetMap. When you save one, we need to remember *which* OSM place it was,
so the map can stop suggesting something already in your list. Matching on name
and rough position would be a guess; an id is an answer.

    source      'manual' (you typed it) or 'osm' (you saved a suggestion)
    source_ref  the OSM element id, e.g. "node/1234567". NULL for manual places.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
# This is the link in the chain. Alembic walks down_revision backwards to work
# out what to apply, which is why 0002 must name 0001 explicitly.
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- the additive-migration lesson ----------------------------------
    #
    # `source` is NOT NULL, and the table already contains rows. Postgres has
    # to put *something* in that column for every existing row, right now, or
    # the NOT NULL constraint would be violated the instant it is added.
    #
    # server_default is what answers that question. It backfills every existing
    # row with 'manual' — which is exactly right, since every place saved before
    # this migration was indeed typed in by hand.
    #
    # Drop the server_default and this migration fails on any non-empty table
    # with:
    #     column "source" of relation "places" contains null values
    # That error is the single most common way a migration breaks in staging
    # after passing on an empty local database.
    op.add_column(
        "places",
        sa.Column(
            "source",
            sa.String(),
            nullable=False,
            server_default="manual",
        ),
    )

    # Nullable, so it needs no backfill and no default: existing rows get NULL,
    # which is the correct value for a place that did not come from OSM.
    op.add_column("places", sa.Column("source_ref", sa.String(), nullable=True))

    op.create_check_constraint(
        "ck_places_source",
        "places",
        "source IN ('manual', 'osm')",
    )

    # The same both-or-neither shape as ck_places_coords_both_or_neither in
    # 0001: an OSM place must carry its reference, and a manual place must not
    # invent one. Written as an equality between two booleans, which is exact —
    # it rules out both "osm with no ref" and "manual with a ref".
    op.create_check_constraint(
        "ck_places_source_ref_present",
        "places",
        "(source = 'osm') = (source_ref IS NOT NULL)",
    )

    # Stops the same OSM place being saved twice to one destination. routes.py
    # catches the resulting IntegrityError and returns 409 Conflict.
    #
    # The surprising part, and worth remembering: this index does **not** need
    # to be partial (`WHERE source_ref IS NOT NULL`). In Postgres a unique index
    # treats NULLs as distinct from each other — NULLS DISTINCT is the default —
    # so every manual place has source_ref NULL and none of them ever collide.
    # Only real OSM references are actually constrained. Most people expect the
    # opposite and reach for a partial index they do not need.
    op.create_index(
        "ix_places_destination_source_ref",
        "places",
        ["destination_id", "source_ref"],
        unique=True,
    )


def downgrade() -> None:
    """Reverse upgrade(). Try it: `alembic downgrade -1`.

    Reverse order again — the index and constraints reference the columns, so
    they have to go before the columns do.
    """
    op.drop_index("ix_places_destination_source_ref", table_name="places")
    op.drop_constraint("ck_places_source_ref_present", "places", type_="check")
    op.drop_constraint("ck_places_source", "places", type_="check")
    op.drop_column("places", "source_ref")
    op.drop_column("places", "source")
