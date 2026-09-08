from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260317_0040"
down_revision = "20260317_0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 一体化平台的市场可见性：listed = 对外白标市场；test = 策略实验市场。
    # 迁移时按"是否有 Paper 流动性配置"推断：产品市场的配置行来自
    # 上币/种子流程，沙盒市场没有该行。
    op.add_column("markets", sa.Column("visibility", sa.String(length=16), nullable=True))
    op.execute(
        "UPDATE markets SET visibility = 'listed' "
        "WHERE id IN (SELECT market_id FROM paper_liquidity_configs)"
    )
    op.execute("UPDATE markets SET visibility = 'test' WHERE visibility IS NULL")


def downgrade() -> None:
    op.drop_column("markets", "visibility")
