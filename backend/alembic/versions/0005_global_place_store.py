"""make the place cache global, and record where each region covers

osm_places was keyed by (destination_id, source_ref), so overlapping cities
stored the same place twice. after this there's one row per osm place, shared
by every destination. osm_extracts also gains bounds, so the api can tell
"nothing here" from "never imported".

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
    # 1. region per place, backfilled from its destination.
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

    # no region means unknown provenance; drop it, the importer refills it.
    op.execute(sa.text("DELETE FROM osm_places WHERE region IS NULL"))

    # 2. remove duplicates before adding the unique constraint. keeps the
    # lowest id of each group, so the result is deterministic.
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

    # 3. drop the destination scoping.
    op.drop_index("ix_osm_places_destination_coords", table_name="osm_places")
    op.drop_constraint(
        "uq_osm_places_destination_ref", "osm_places", type_="unique"
    )
    # drops the foreign key too.
    op.drop_column("osm_places", "destination_id")
    op.alter_column("osm_places", "region", nullable=False)

    # 4. the global key and the viewport index.
    op.create_unique_constraint(
        "uq_osm_places_source_ref", "osm_places", ["source_ref"]
    )
    op.create_index(
        "ix_osm_places_coords", "osm_places", ["latitude", "longitude"]
    )
    op.create_index("ix_osm_places_region", "osm_places", ["region"])

    # 5. region ids now come from geofabrik's index ("malta", "us/new-york"),
    # so strip the old continent prefixes.
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

    # 6. region bounds, for coverage. nullable: unknown until the download starts.
    for column in ("south", "west", "north", "east"):
        op.add_column(
            "osm_extracts", sa.Column(column, sa.Float(), nullable=True)
        )


def downgrade() -> None:
    """back to per-destination storage.

    the deleted duplicates and each row's destination can't be recovered, so
    the cache is emptied and the importer refills it.
    """
    for column in ("east", "north", "west", "south"):
        op.drop_column("osm_extracts", column)

    op.drop_index("ix_osm_places_region", table_name="osm_places")
    op.drop_index("ix_osm_places_coords", table_name="osm_places")
    op.drop_constraint("uq_osm_places_source_ref", "osm_places", type_="unique")

    # empty first: there's no valid destination_id for existing rows.
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
