"""Configure multiple payment methods and custom recharge credit rates."""
from alembic import op
import sqlalchemy as sa


revision = "0050_payment_methods_and_custom_rate"
down_revision = "0049_payment_recharge_options"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("site_payment_configs", sa.Column(
        "payment_methods", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("site_payment_configs", sa.Column(
        "custom_credits_per_yuan", sa.Integer(), nullable=False, server_default="100"))
    op.execute(sa.text("""
        UPDATE site_payment_configs
        SET payment_methods = CASE
            WHEN pay_type <> '' THEN json_build_array(json_build_object(
                'code', pay_type,
                'label', CASE lower(pay_type)
                    WHEN 'alipay' THEN '支付宝'
                    WHEN 'wxpay' THEN '微信支付'
                    WHEN 'wechat' THEN '微信支付'
                    WHEN 'qqpay' THEN 'QQ 钱包'
                    WHEN 'unionpay' THEN '银联'
                    WHEN 'usdt' THEN 'USDT'
                    ELSE pay_type
                END
            ))
            ELSE '[]'::json
        END
    """))


def downgrade():
    op.drop_column("site_payment_configs", "custom_credits_per_yuan")
    op.drop_column("site_payment_configs", "payment_methods")
