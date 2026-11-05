"""Separate model capabilities from supplier credentials."""
from alembic import op
import sqlalchemy as sa

revision = "0054_model_capabilities"
down_revision = "0053_resource_library"
branch_labels = depends_on = None


def upgrade():
    op.drop_constraint("ck_model_connection_protocol", "model_connections", type_="check")
    op.create_check_constraint("ck_model_connection_protocol", "model_connections", "api_protocol IN ('chat_completions', 'responses', 'anthropic', 'gemini')")
    op.add_column("model_connections", sa.Column("website_url", sa.Text(), nullable=False, server_default=""))
    op.add_column("site_model_providers", sa.Column("chat_protocol", sa.String(30), nullable=False, server_default="chat_completions"))
    op.add_column("site_model_providers", sa.Column("supports_chat", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("site_model_providers", sa.Column("image_config", sa.JSON(), nullable=False, server_default="{}"))
    op.execute("UPDATE site_model_providers SET chat_protocol = model_connections.api_protocol FROM model_connections WHERE site_model_providers.connection_id = model_connections.id")


def downgrade():
    op.execute("UPDATE model_connections SET api_protocol = 'chat_completions' WHERE api_protocol IN ('anthropic', 'gemini')")
    op.drop_constraint("ck_model_connection_protocol", "model_connections", type_="check")
    op.create_check_constraint("ck_model_connection_protocol", "model_connections", "api_protocol IN ('chat_completions', 'responses')")
    op.drop_column("site_model_providers", "image_config")
    op.drop_column("site_model_providers", "supports_chat")
    op.drop_column("site_model_providers", "chat_protocol")
    op.drop_column("model_connections", "website_url")
