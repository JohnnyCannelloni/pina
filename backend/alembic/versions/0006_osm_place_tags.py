"""keep the useful OpenStreetMap tags on each cached place

one jsonb column rather than a column per tag, so adding a tag needs no
migration. the gin index (jsonb_path_ops) serves the `tags @> ...` filters in
app/ask.py. existing rows have no tags until their region is re-imported.

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
