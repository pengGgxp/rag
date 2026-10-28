"""Store requested thinking settings and private protocol continuation."""
from alembic import op
import sqlalchemy as sa

revision = "0028_message_thinking"
down_revision = "0027_model_thinking"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column("thinking_snapshot", sa.JSON(), nullable=True))
    op.add_column("chat_messages", sa.Column("encrypted_continuation", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("chat_messages", "encrypted_continuation")
    op.drop_column("chat_messages", "thinking_snapshot")
