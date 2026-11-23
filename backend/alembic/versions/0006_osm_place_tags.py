"""keep the useful OpenStreetMap tags on each cached place

Until now a cached place was a name, a category and a pin. That is enough to
draw a map, but not to answer "Italian, with outdoor seating" — which is what
the Ask Pina search (app/ask.py) needs, and what the map popups want to show.

**One JSONB column, not a column per tag.** The tags worth keeping are a
whitelist in app/extract.py (cuisine, diet, outdoor seating, hours, website...).
That list will grow, and with JSONB adding a tag is an importer change plus a
re-import, never a migration. The trade: Postgres cannot check the shape of the
JSON for us, so the importer is the one place that decides what goes in.

**The GIN index** is what makes `tags @> '{"outdoor_seating": "yes"}'` and
`tags -> 'cuisine' ?| array['italian']` index lookups rather than a scan of
every place in the country. `jsonb_path_ops` is the smaller, faster GIN flavour;
it supports `@>`, which is how every filter in app/ask.py is written.

Nullable with no default: existing rows simply have no tags until their region
is re-imported (`python -m app.fill_places --region <region>`), which reads the
cached file and downloads nothing.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "osm_places",
        sa.Column("tags", postgresql.JSONB(), nullable=True),
    )
    op.create_index(
        "ix_osm_places_tags",
        "osm_places",
        ["tags"],
        postgresql_using="gin",
        postgresql_ops={"tags": "jsonb_path_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_osm_places_tags", table_name="osm_places")
    op.drop_column("osm_places", "tags")
