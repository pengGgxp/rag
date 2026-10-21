"""Preserve parser state and PDF source locations without changing old documents."""

import sqlalchemy as sa
from alembic import op

revision = "0014_add_parser_locations"
down_revision = "0013_add_chunk_fts_index"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("documents", sa.Column("parsing_metadata", sa.JSON(), nullable=True))
    op.add_column("chunks", sa.Column("source_locations", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    op.drop_column("chunks", "source_locations")
    op.drop_column("documents", "parsing_metadata")
