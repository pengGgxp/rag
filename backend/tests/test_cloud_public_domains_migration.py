import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_remove_old_public_domain_defaults_preserves_other_rules():
    path = Path(__file__).parents[1]/"alembic/versions/0057_cloud_public_domains.py"
    spec = importlib.util.spec_from_file_location("cloud_public_domains_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table("cloud_computer_settings", metadata, sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("version", sa.Integer), sa.Column("config", sa.JSON))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert().values(id=1, version=4, config={
            "blocked_hosts": ["zk.str1ct.top", "q.str1ct.top", "example.org", "203.0.113.0/24"],
            "credits_per_minute": "1"}))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
        row = connection.execute(sa.select(table)).mappings().one()
        assert row["version"] == 5
        assert row["config"] == {"blocked_hosts": ["example.org", "203.0.113.0/24"], "credits_per_minute": "1"}
    engine.dispose()
