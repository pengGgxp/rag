"""add cosine index for chunk embeddings

Revision ID: 0004_add_embedding_search_index
Revises: 0003_switch_embeddings_to_1024
Create Date: 2026-10-20
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0004_add_embedding_search_index"
down_revision: Union[str, None] = "0003_switch_embeddings_to_1024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_chunks_embedding_cosine "
        "ON chunks USING hnsw (embedding vector_cosine_ops) "
        "WHERE embedding IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_cosine")
