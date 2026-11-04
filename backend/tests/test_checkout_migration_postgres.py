"""Exercise the checkout migration against disposable PostgreSQL schemas."""
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url


@pytest.fixture
def migration_db():
    url = os.environ.get("PAYMENTS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires disposable PAYMENTS_TEST_DATABASE_URL")
    if not (make_url(url).database or "").startswith("rag_recharge_test_"):
        pytest.fail("refusing to run against a non-test database")
    engine = sa.create_engine(url)
    with engine.connect() as conn, conn.begin():
        schema = "checkout_" + uuid4().hex
        conn.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
        metadata = sa.MetaData()
        sa.Table("users", metadata, sa.Column("id", sa.UUID, primary_key=True))
        configs = sa.Table("site_payment_configs", metadata,
            sa.Column("version", sa.Integer, primary_key=True), sa.Column("provider_key", sa.String),
            sa.Column("enabled", sa.Boolean), sa.Column("packages", sa.JSON), sa.Column("payment_methods", sa.JSON),
            sa.Column("custom_recharge_enabled", sa.Boolean), sa.Column("custom_credits_per_yuan", sa.Integer),
            sa.Column("created_by", sa.UUID))
        orders = sa.Table("recharge_orders", metadata, sa.Column("id", sa.UUID, primary_key=True),
            sa.Column("amount_fen", sa.Integer), sa.Column("credits", sa.Integer), sa.Column("pay_type", sa.String))
        ledger = sa.Table("credit_ledger_entries", metadata, sa.Column("id", sa.UUID, primary_key=True), sa.Column("amount", sa.Integer))
        metadata.create_all(conn)
        conn.execute(orders.insert().values(id=uuid4(), amount_fen=1200, credits=1500, pay_type="legacy"))
        conn.execute(ledger.insert().values(id=uuid4(), amount=1500))
        user_id = uuid4()
        conn.execute(metadata.tables["users"].insert().values(id=user_id))
        spec = importlib.util.spec_from_file_location("checkout_migration", Path(__file__).parents[1] / "alembic/versions/0051_recharge_checkout.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        def migrate():
            with Operations.context(MigrationContext.configure(conn)):
                module.upgrade()
        yield conn, configs, user_id, migrate
        # The schema only contains synthetic data and is removed with this transaction.
        conn.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()


def add_config(conn, table, actor, version, provider, *, enabled=True, credits=1500):
    conn.execute(table.insert().values(version=version, provider_key=provider, enabled=enabled,
        packages=[{"id": "site", "name": "标准档", "amount_fen": 1200, "credits": credits}],
        payment_methods=[{"code": "wxpay", "label": "微信支付"}],
        custom_recharge_enabled=True, custom_credits_per_yuan=250, created_by=actor))


def test_migration_preserves_prices_history_and_latest_provider_versions(migration_db):
    conn, configs, actor, migrate = migration_db
    add_config(conn, configs, actor, 1, "zhifux", enabled=True, credits=999)
    add_config(conn, configs, actor, 2, "zhifux", enabled=False, credits=999)
    add_config(conn, configs, actor, 3, "ezfp")
    before = conn.execute(sa.text("SELECT id, amount_fen, credits, pay_type FROM recharge_orders")).all()
    migrate()
    row = conn.execute(sa.text("SELECT * FROM site_recharge_configs")).mappings().one()
    assert row["packages"][0]["credits"] == 1500 and row["custom_credits_per_yuan"] == 250
    assert row["payment_methods"] == [{"id": "ezfp_wxpay", "label": "微信支付", "code": "wxpay", "provider_key": "ezfp", "enabled": True}]
    assert conn.execute(sa.text("SELECT id, amount_fen, credits, pay_type FROM recharge_orders")).all() == before
    assert conn.scalar(sa.text("SELECT amount FROM credit_ledger_entries")) == 1500
    assert conn.scalar(sa.text("SELECT count(*) FROM site_payment_configs")) == 3


def test_migration_conflicting_active_prices_stops_before_ddl(migration_db):
    conn, configs, actor, migrate = migration_db
    add_config(conn, configs, actor, 1, "zhifux", credits=123)
    add_config(conn, configs, actor, 2, "ezfp")
    with pytest.raises(RuntimeError, match="报价不一致"):
        migrate()
    assert not sa.inspect(conn).has_table("site_recharge_configs")
    assert len(sa.inspect(conn).get_columns("recharge_orders")) == 4
    conn.execute(configs.update().where(configs.c.provider_key == "zhifux").values(enabled=False))
    migrate()
    assert conn.scalar(sa.text("SELECT count(*) FROM site_recharge_configs")) == 1


def test_migration_identical_active_quotes_retains_explicit_routes(migration_db):
    conn, configs, actor, migrate = migration_db
    add_config(conn, configs, actor, 1, "zhifux")
    add_config(conn, configs, actor, 2, "ezfp")
    migrate()
    methods = conn.scalar(sa.text("SELECT payment_methods FROM site_recharge_configs"))
    assert {m["id"] for m in methods} == {"ezfp_wxpay", "zhifux_wxpay"}


def test_migration_disabled_only_does_not_enable_payments(migration_db):
    conn, configs, actor, migrate = migration_db
    add_config(conn, configs, actor, 1, "ezfp", enabled=False)
    migrate()
    assert conn.scalar(sa.text("SELECT payment_methods FROM site_recharge_configs")) == []
