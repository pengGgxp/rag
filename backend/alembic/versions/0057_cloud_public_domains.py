"""Allow public application domains in cloud execution by default."""
from alembic import op
import sqlalchemy as sa

revision = "0057_cloud_public_domains"
down_revision = "0056_cloud_computer"
branch_labels = depends_on = None


def upgrade():
    table = sa.table("cloud_computer_settings", sa.column("id", sa.Integer),
        sa.column("version", sa.Integer), sa.column("config", sa.JSON))
    connection = op.get_bind()
    for row in connection.execute(sa.select(table)).mappings():
        config = dict(row["config"])
        previous = config.get("blocked_hosts", [])
        hosts = [host for host in previous if host.lower().rstrip(".") not in {"zk.str1ct.top", "q.str1ct.top"}]
        if hosts != previous:
            config["blocked_hosts"] = hosts
            connection.execute(table.update().where(table.c.id == row["id"]).values(
                config=config, version=row["version"]+1))


def downgrade():
    # Do not reintroduce restrictions or overwrite administrator network choices.
    pass
