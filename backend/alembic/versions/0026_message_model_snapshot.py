"""Record the actual model on each assistant response."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0026_message_model"
down_revision = "0025_workspace_billing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_messages", sa.Column("provider_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("chat_messages", sa.Column("provider_name", sa.String(120), nullable=True))
    op.add_column("chat_messages", sa.Column("model", sa.String(200), nullable=True))
    op.create_foreign_key("fk_chat_message_provider", "chat_messages", "llm_provider_configs",
                          ["provider_id"], ["id"])


def downgrade():
    op.drop_constraint("fk_chat_message_provider", "chat_messages", type_="foreignkey")
    op.drop_column("chat_messages", "model")
    op.drop_column("chat_messages", "provider_name")
    op.drop_column("chat_messages", "provider_id")
