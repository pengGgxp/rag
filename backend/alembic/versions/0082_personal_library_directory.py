"""Link personal knowledge bases to their mounted directories."""
from alembic import op
import sqlalchemy as sa

revision = '0082_personal_library_directory'
down_revision = '0081_direct_account_ownership'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("document_uploads", sa.Column("relative_path", sa.String(512)))
    op.add_column("documents", sa.Column("personal_file_id", sa.Uuid(), sa.ForeignKey("workspace_files.id"), unique=True))
    op.add_column('knowledge_bases', sa.Column('directory_path', sa.String(512)))
    op.create_index('uq_personal_library_path', 'knowledge_bases', ['account_id', 'directory_path'], unique=True)


def downgrade():
    op.drop_column("document_uploads", "relative_path")
    op.drop_column("documents", "personal_file_id")
    op.drop_index('uq_personal_library_path', table_name='knowledge_bases')
    op.drop_column('knowledge_bases', 'directory_path')
