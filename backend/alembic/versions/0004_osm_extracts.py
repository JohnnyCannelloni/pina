"""replace the Overpass tile sync with regional OSM extracts

Why this migration exists: the previous design fetched a destination's places
from the public Overpass API, tile by tile, at the moment you needed them.
Measured over an afternoon, that API returned 502 "all query workers are busy",
a 25-second timeout, or 429 on the large majority of attempts. A feature built
on it did not work.

The replacement gets the same OpenStreetMap data from a completely different
place: Geofabrik publishes the whole planet as static regional files. Static
file hosting has no query workers to be busy and no rate limit to exceed. One
~250 MB download per *country* is parsed locally in seconds — measured on
Malta: 1,749 named places in under a second.

So:
  - `destinations` learns which country (and therefore which extract) it needs.
  - `osm_extracts` tracks each downloaded region, so a second city in the same
    country costs no download at all.
  - `osm_tiles` is deleted. It existed only to make a flaky per-tile API
    resumable; with a static file there are no tiles and nothing to resume.

`osm_places` is unchanged. That is the point of having separated the cache from
the collection back in 0003 — the *source* of the cache changed completely and
neither the table the map reads nor your saved places had to move.

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
    # --- destinations: which extract covers this place -------------------
    #
    # Both nullable, and deliberately so. The seeded Venice predates geocoding
    # and has no country code; a destination whose country we cannot map to a
    # Geofabrik region is still a perfectly good destination that you can save
    # places to by hand. Making these NOT NULL would turn an unsupported region
    # into an unusable destination.
    op.add_column(
        "destinations", sa.Column("country_code", sa.String(), nullable=True)
    )
    # The Geofabrik path, e.g. "europe/portugal". Stored rather than derived on
    # every read so that a change to the country->region table cannot silently
    # repoint an already-imported destination at a different extract.
    op.add_column("destinations", sa.Column("region", sa.String(), nullable=True))
    op.create_index("ix_destinations_region", "destinations", ["region"])

    # --- osm_extracts: one row per downloaded region ---------------------
    op.create_table(
        "osm_extracts",
        sa.Column("id", sa.Integer(), primary_key=True),
        # "europe/malta". Unique: this is the whole point of the table — to know
        # that Portugal is already imported so adding Porto after Lisbon costs
        # nothing.
        sa.Column("region", sa.String(), nullable=False),
        sa.Column(
            "state", sa.String(), nullable=False, server_default="pending"
        ),
        # How many places this extract contributed. Not a strict count of rows
        # in osm_places, because two destinations in one country draw from the
        # same extract — it is for telling the user something happened.
        sa.Column(
            "place_count", sa.Integer(), nullable=False, server_default="0"
        ),
        # BigInteger, not Integer: a planet-scale extract exceeds 2^31 bytes and
        # Integer would overflow. Cheap insurance against a column type that is
        # painful to widen later.
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

    # --- drop the tile queue --------------------------------------------
    #
    # Deleted rather than left unused. A second, non-working way to fill the
    # same table is worse than no second way: it is code nobody runs, that
    # nothing tests, and that the next reader has to work out is dead.
    op.drop_index("ix_osm_tiles_destination_state", table_name="osm_tiles")
    op.drop_table("osm_tiles")

    # Any places already cached came from the Overpass path, in whatever partial
    # state its failures left them. Clearing them means the first import gives a
    # clean, complete picture rather than a mix of two sources. Safe: this is a
    # cache, and `places` — the collection you curated — is untouched.
    op.execute(sa.text("DELETE FROM osm_places"))


def downgrade() -> None:
    """Recreate the tile queue and drop the extract tracking.

    The recreated osm_tiles is empty, which is correct: the tiles it held
    described work for an API this schema version no longer uses.
    """
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
