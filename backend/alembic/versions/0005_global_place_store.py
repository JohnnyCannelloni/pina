"""make the place cache global, and record where each region covers

READ THIS FIRST. It contains the single most common awkward migration shape:
**you cannot add a unique constraint until you have deleted the duplicates**,
and the duplicates only exist because of the design you are migrating away from.

Why the change. `osm_places` was keyed `(destination_id, source_ref)` — a
separate copy of every place for every destination. Two consequences, both bad
once there is more than one destination:

  - Overlapping cities store the same café twice. Venice and Mestre share
    streets; both got their own row for the same OSM node.
  - Every new destination re-imports data the server already has, because the
    rows are filed under someone else's id.

A reference dataset — "what exists in the world" — is not per-user data. It
should be stored once and shared. After this migration there is exactly one row
per OpenStreetMap place, and any destination, trip, or future client reads the
same rows.

The second, smaller change: `osm_extracts` learns where each region *is*, so the
API can tell "there is nothing here" apart from "this area was never imported".
That ambiguity is what made earlier versions of the map feel broken.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. provenance, backfilled from a JOIN ---------------------------
    #
    # Nullable first, as in 0003 and 0004. But note the difference from those:
    # there the backfill value was computed from the row itself. Here it has to
    # come from a *different table* — the region is a property of the
    # destination the places were imported for.
    #
    # That is why this is an UPDATE ... FROM rather than a server_default: no
    # constant could be right, because the answer differs per row and lives
    # elsewhere.
    op.add_column("osm_places", sa.Column("region", sa.String(), nullable=True))
    op.execute(
        sa.text(
            """
            UPDATE osm_places AS p
               SET region = d.region
              FROM destinations AS d
             WHERE d.id = p.destination_id
               AND d.region IS NOT NULL
            """
        )
    )

    # A place whose destination had no region tells us nothing about where its
    # data came from. Deleted rather than guessed at — this is a cache, and the
    # importer will put it back. Guessing provenance is worse than re-fetching.
    op.execute(sa.text("DELETE FROM osm_places WHERE region IS NULL"))

    # --- 2. collapse duplicates BEFORE the unique constraint -------------
    #
    # This statement is the whole reason the migration is interesting. Step 4
    # adds UNIQUE (source_ref); if two destinations each hold node/123, that
    # ALTER fails with:
    #     could not create unique index "uq_osm_places_source_ref"
    #     DETAIL: Key (source_ref)=(node/123) is duplicated.
    #
    # DELETE ... USING is Postgres's self-join delete. `a.id > b.id` keeps the
    # lowest id of each group: an arbitrary but *deterministic* winner, which
    # matters because the rows are otherwise identical and a non-deterministic
    # choice would make this migration unrepeatable.
    op.execute(
        sa.text(
            """
            DELETE FROM osm_places AS a
                  USING osm_places AS b
                  WHERE a.source_ref = b.source_ref
                    AND a.id > b.id
            """
        )
    )

    # --- 3. drop the destination scoping --------------------------------
    op.drop_index("ix_osm_places_destination_coords", table_name="osm_places")
    op.drop_constraint(
        "uq_osm_places_destination_ref", "osm_places", type_="unique"
    )
    # Dropping the column drops its foreign key with it. The rows survive the
    # deletion of a destination now, which is correct: the world does not stop
    # containing a restaurant because you cancelled a trip.
    op.drop_column("osm_places", "destination_id")
    op.alter_column("osm_places", "region", nullable=False)

    # --- 4. now the global key, and the index the map reads through ------
    op.create_unique_constraint(
        "uq_osm_places_source_ref", "osm_places", ["source_ref"]
    )
    # The index behind every viewport query. No destination_id to lead with any
    # more, so latitude leads: Postgres range-scans it and filters longitude
    # from the index rows. Good enough at this scale; the answer at very large
    # scale is PostGIS with a GiST index on a geography column, which this
    # project deliberately does not need yet.
    op.create_index(
        "ix_osm_places_coords", "osm_places", ["latitude", "longitude"]
    )
    # Lets a region be re-imported or removed wholesale.
    op.create_index("ix_osm_places_region", "osm_places", ["region"])

    # --- 5. normalise region identifiers --------------------------------
    #
    # The region id scheme changed with this revision. The old hand-written
    # table used continent-prefixed paths ("europe/malta", "north-america/us");
    # Geofabrik's own index — now the source of truth — identifies the same
    # regions as "malta" and "us", and nests only where it actually subdivides
    # ("us/new-york").
    #
    # Left alone, stored values like "europe/malta" would no longer be found in
    # the index, and every affected destination would silently fail to refill.
    # So the prefix is stripped here. A data migration for a *naming* change,
    # which is less common than a schema change and easy to forget entirely —
    # the symptom would appear much later, as "region not found".
    continents = (
        "africa",
        "antarctica",
        "asia",
        "australia-oceania",
        "central-america",
        "europe",
        "north-america",
        "south-america",
    )
    for table in ("osm_places", "osm_extracts", "destinations"):
        for continent in continents:
            op.execute(
                sa.text(
                    f"""
                    UPDATE {table}
                       SET region = substring(region from :cut)
                     WHERE region LIKE :prefix
                    """
                ).bindparams(
                    cut=len(continent) + 2,  # 1-based, past the slash
                    prefix=f"{continent}/%",
                )
            )

    # --- 6. coverage: where an imported region actually is ---------------
    #
    # Nullable, because an extract row can exist before its bounds are known
    # (it is created when a download starts). The API treats "no bounds" as
    # "cannot confirm coverage", which is honest rather than optimistic.
    for column in ("south", "west", "north", "east"):
        op.add_column(
            "osm_extracts", sa.Column(column, sa.Float(), nullable=True)
        )


def downgrade() -> None:
    """Return to per-destination storage.

    The honest caveat: this **cannot restore the duplicates** upgrade() deleted,
    and it cannot know which destination each remaining row belonged to. So it
    empties the cache and lets the importer refill it. A downgrade that
    fabricated plausible destination_ids would be worse than one that admits the
    information is gone.

    This is a real property of destructive migrations, and worth seeing once:
    reversible schema does not mean reversible data.
    """
    for column in ("east", "north", "west", "south"):
        op.drop_column("osm_extracts", column)

    op.drop_index("ix_osm_places_region", table_name="osm_places")
    op.drop_index("ix_osm_places_coords", table_name="osm_places")
    op.drop_constraint("uq_osm_places_source_ref", "osm_places", type_="unique")

    # Empty it before re-adding the NOT NULL foreign key, since there is no
    # value we could legitimately put in destination_id for existing rows.
    op.execute(sa.text("DELETE FROM osm_places"))

    op.drop_column("osm_places", "region")
    op.add_column(
        "osm_places", sa.Column("destination_id", sa.Integer(), nullable=False)
    )
    op.create_foreign_key(
        "fk_osm_places_destination_id",
        "osm_places",
        "destinations",
        ["destination_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_osm_places_destination_ref",
        "osm_places",
        ["destination_id", "source_ref"],
    )
    op.create_index(
        "ix_osm_places_destination_coords",
        "osm_places",
        ["destination_id", "latitude", "longitude"],
    )
