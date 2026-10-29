"""Make the upstream API protocol explicit without changing existing routing."""
from alembic import op
import sqlalchemy as sa

revision = "0034_model_api_protocol"
down_revision = "0033_model_connections"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("model_connections", sa.Column("api_protocol", sa.String(30),
        nullable=False, server_default="chat_completions"))
    op.create_check_constraint("ck_model_connection_protocol", "model_connections",
        "api_protocol IN ('chat_completions', 'responses')")


def downgrade():
    op.drop_constraint("ck_model_connection_protocol", "model_connections", type_="check")
    op.drop_column("model_connections", "api_protocol")
