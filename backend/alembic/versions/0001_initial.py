"""initial schema: trips, destinations, places

START HERE. This file creates the tables a saved place lands in. Everything else
in the backend is machinery for getting rows into and out of these three tables.

Shape:

    trips          one row:  Italy
      |
      | trip_id (FK, ON DELETE CASCADE)
      v
    destinations   one row:  Venice, with the coordinates the map centres on
      |
      | destination_id (FK, ON DELETE CASCADE)
      v
    places         every restaurant / cafe / sight you save,
                   with optional latitude+longitude = the dropped pin

This migration is hand-written rather than produced by
`alembic revision --autogenerate`. Autogenerate diffs the models against the
database and is genuinely useful later, but its output reads like a diff, not
like an explanation — and it is easy to let it write something you have not
understood. For the first migration, writing it yourself is the exercise.

Revision ID: 0001
Revises: (nothing — this is the first)
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic identifies migrations by these strings and chains them with
# down_revision, forming a linked list it walks to decide what to apply.
# down_revision = None marks this as the head of the chain.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- trips ------------------------------------------------------------
    op.create_table(
        "trips",
        # Integer primary key. In Postgres this becomes an identity column, so
        # the database assigns 1, 2, 3... and you never pick an id yourself.
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            # timezone=True is DOUBLE PRECISION's equivalent decision for time:
            # it gives TIMESTAMPTZ, which stores an unambiguous instant.
            # Plain TIMESTAMP stores wall-clock digits with no timezone, and is
            # the source of a whole genre of bug. Always use timezone=True.
            sa.DateTime(timezone=True),
            nullable=False,
            # server_default puts DEFAULT now() in the table itself, so rows
            # inserted by psql or any future script get a timestamp too — not
            # just rows that happen to go through the ORM.
            server_default=sa.text("now()"),
        ),
    )

    # --- destinations -----------------------------------------------------
    op.create_table(
        "destinations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("trip_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        # NOT NULL: the map needs a centre, and where Venice is, is a fact about
        # Venice. Storing it here rather than as a constant in React is what
        # makes adding a second city a seed change instead of a code change.
        #
        # Float() in Postgres is DOUBLE PRECISION — IEEE-754 float64. The same
        # type JavaScript numbers and Python floats use, so a coordinate makes
        # the round trip browser -> JSON -> Postgres -> JSON -> browser without
        # losing a single bit. That exactness is why the pin reappears in
        # precisely the same spot after a refresh, and why the integration test
        # can assert plain equality on it.
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
            # ON DELETE CASCADE: delete a trip and Postgres deletes its
            # destinations (and, through the next cascade, their places). The
            # alternative default, NO ACTION, would refuse the delete and leave
            # you to clean up by hand.
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_destinations_trip_id", "destinations", ["trip_id"]
    )

    # --- places -----------------------------------------------------------
    # The table this whole feature exists to write to. One row per saved place;
    # the list view shows all of them and the map view shows the subset with
    # coordinates. One table, two views — nothing to keep in sync.
    op.create_table(
        "places",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("destination_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        # Stored as text and policed by ck_places_category below, rather than as
        # a Postgres native ENUM type. Both reject bad data. The difference is
        # what happens when the LLM feature wants to add "bar": here it is a
        # one-line constraint swap, whereas a native enum needs
        # `ALTER TYPE ... ADD VALUE`, which historically could not run inside a
        # transaction. Pick the one that is cheaper to change.
        sa.Column("category", sa.String(), nullable=False),
        sa.Column(
            "must_go",
            sa.Boolean(),
            nullable=False,
            # NOT NULL plus a default, so the column is a clean two-valued flag.
            # Nullable booleans give you three states and a decision to make
            # every time you read one.
            server_default=sa.text("false"),
        ),
        # NULLABLE, unlike the destination's coordinates: a place saved without
        # a pin is a normal, supported thing. It simply does not appear on the
        # map — it still appears in the list.
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
        # Every constraint below is explicitly named. Left unnamed, Postgres
        # invents something like places_category_check — which makes error
        # messages vague and makes dropping it in downgrade() a guess.
        sa.CheckConstraint(
            "category IN ('restaurant', 'cafe', 'sight')",
            name="ck_places_category",
        ),
        # The interesting one. A place has a pin or it does not; one coordinate
        # without the other is meaningless data that would put a marker on the
        # equator. Expressing it as `(a IS NULL) = (b IS NULL)` reads oddly at
        # first but is exact: both null, or neither.
        #
        # PlaceCreate in schemas.py checks the same thing, to produce a helpful
        # 422. That is not redundancy to delete — app validation gives good
        # error messages, a constraint makes the rule actually true. This copy
        # holds even for a future endpoint, a migration, or a stray psql INSERT.
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)",
            name="ck_places_coords_both_or_neither",
        ),
        # `latitude IS NULL OR ...` is required: a CHECK whose expression
        # evaluates to NULL *passes*, but being explicit about the no-pin case
        # states the intent instead of relying on that rule.
        sa.CheckConstraint(
            "latitude IS NULL OR (latitude BETWEEN -90 AND 90)",
            name="ck_places_lat_range",
        ),
        sa.CheckConstraint(
            "longitude IS NULL OR (longitude BETWEEN -180 AND 180)",
            name="ck_places_lng_range",
        ),
    )
    # Both place endpoints filter on destination_id, so it gets an index. With
    # a dozen rows Postgres will ignore it and do a sequential scan anyway; it
    # is here because "index the column you filter on" is the habit, and the
    # cost of adding it now is nothing.
    op.create_index("ix_places_destination_id", "places", ["destination_id"])


def downgrade() -> None:
    """Undo upgrade(), exactly.

    Try it: `alembic downgrade base` empties the database, `alembic upgrade
    head` rebuilds it. A downgrade that does not work is a migration you cannot
    back out of, which is how a bad deploy becomes a long evening.

    Order matters — children before parents. Dropping `destinations` while
    `places` still references it would fail on the foreign key. This is
    upgrade() in reverse.
    """
    op.drop_index("ix_places_destination_id", table_name="places")
    op.drop_table("places")
    op.drop_index("ix_destinations_trip_id", table_name="destinations")
    op.drop_table("destinations")
    op.drop_table("trips")
