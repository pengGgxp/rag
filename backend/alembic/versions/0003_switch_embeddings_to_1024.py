"""switch chunk embeddings to 1024 dimensions

Revision ID: 0003_switch_embeddings_to_1024
Revises: 0002_add_chunks
Create Date: 2026-10-20
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


revision: str = "0003_switch_embeddings_to_1024"
down_revision: Union[str, None] = "0002_add_chunks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    embedding_status = sa.Enum(
        "PENDING",
        "RUNNING",
        "READY",
        "FAILED",
        name="embeddingstatus",
    )
    embedding_status.create(bind, checkfirst=True)

    # A pgvector column cannot be cast from 1536 to 1024. Replace only the
    # vector payload while preserving every existing document chunk.
    op.add_column("chunks", sa.Column("embedding_new", Vector(1024), nullable=True))
    op.drop_column("chunks", "embedding")
    op.alter_column("chunks", "embedding_new", new_column_name="embedding")
    op.add_column(
        "chunks",
        sa.Column(
            "embedding_status",
            embedding_status,
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
    )
    op.add_column("chunks", sa.Column("embedding_error", sa.Text(), nullable=True))
    op.alter_column("chunks", "embedding_status", server_default=None)


def downgrade() -> None:
    op.add_column("chunks", sa.Column("embedding_new", Vector(1536), nullable=True))
    op.drop_column("chunks", "embedding")
    op.alter_column("chunks", "embedding_new", new_column_name="embedding")
    op.drop_column("chunks", "embedding_error")
    op.drop_column("chunks", "embedding_status")
    sa.Enum(name="embeddingstatus").drop(op.get_bind(), checkfirst=True)
