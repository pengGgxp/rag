"""Unify recharge quotes and explicitly route public payment methods."""
import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0051_recharge_checkout"
down_revision = "0050_payment_methods_rates"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    configs = sa.table("site_payment_configs", sa.column("version"), sa.column("provider_key"),
        sa.column("enabled"), sa.column("packages", sa.JSON), sa.column("payment_methods", sa.JSON),
        sa.column("custom_recharge_enabled"), sa.column("custom_credits_per_yuan"), sa.column("created_by"))
    latest = {}
    for row in bind.execute(sa.select(configs).order_by(configs.c.version.desc())).mappings():
        latest.setdefault(row["provider_key"], row)
    active = [row for row in latest.values() if row["enabled"]]
    source = next((row for row in active if row["provider_key"] == "ezfp"), next(iter(active), None))
    if source:
        def pricing(row):
            return (json.dumps(sorted(row["packages"], key=lambda item: item["id"]), sort_keys=True),
                    row["custom_recharge_enabled"], row["custom_credits_per_yuan"])
        if any(pricing(row) != pricing(source) for row in active):
            raise RuntimeError("启用渠道的充值报价不一致，请先确认统一报价并停用非迁移来源渠道，再执行迁移")
    op.create_table("site_recharge_configs",
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("packages", sa.JSON, nullable=False),
        sa.Column("custom_recharge_enabled", sa.Boolean, nullable=False),
        sa.Column("custom_credits_per_yuan", sa.Integer, nullable=False),
        sa.Column("payment_methods", sa.JSON, nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for name, type_ in [("recharge_config_version", sa.Integer()), ("payment_method_id", sa.String(120)),
                        ("payment_method_label", sa.String(60)), ("quote", sa.String(64))]:
        op.add_column("recharge_orders", sa.Column(name, type_, nullable=True))
    if latest:
        # Disabled-only installations retain a draft without exposing any payment method.
        source = source or latest.get("ezfp") or next(iter(latest.values()))
        methods = [{"id": f'{row["provider_key"]}_{method["code"]}', "label": method["label"],
                    "provider_key": row["provider_key"], "code": method["code"], "enabled": True}
                   for row in active for method in row["payment_methods"]]
        table = sa.table("site_recharge_configs", sa.column("version"), sa.column("packages", sa.JSON),
            sa.column("custom_recharge_enabled"), sa.column("custom_credits_per_yuan"),
            sa.column("payment_methods", sa.JSON), sa.column("created_by"))
        bind.execute(table.insert().values(version=1, packages=source["packages"],
            custom_recharge_enabled=source["custom_recharge_enabled"],
            custom_credits_per_yuan=source["custom_credits_per_yuan"], payment_methods=methods,
            created_by=source["created_by"]))


def downgrade():
    for name in ["quote", "payment_method_label", "payment_method_id", "recharge_config_version"]:
        op.drop_column("recharge_orders", name)
    op.drop_table("site_recharge_configs")
