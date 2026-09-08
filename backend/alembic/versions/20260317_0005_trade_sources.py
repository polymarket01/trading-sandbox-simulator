from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0005"
down_revision = "20260317_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trades", sa.Column("source", sa.String(length=32), nullable=False, server_default="unknown"))
    op.add_column("klines", sa.Column("source", sa.String(length=32), nullable=False, server_default="unknown"))
    op.create_index("idx_trades_market_source_time", "trades", ["market_id", "source", "executed_at"], unique=False)
    op.create_index("idx_klines_market_interval_source_time", "klines", ["market_id", "interval", "source", "open_time"], unique=False)

    op.execute(
        """
        UPDATE trades
        SET source = 'bootstrap_seed'
        WHERE taker_order_id IN (SELECT order_id FROM orders WHERE client_order_id LIKE 'seed-%')
           OR maker_order_id IN (SELECT order_id FROM orders WHERE client_order_id LIKE 'seed-%')
        """
    )
    op.execute(
        """
        UPDATE trades
        SET source = 'bot'
        WHERE source = 'unknown'
          AND (
            taker_user_id IN (SELECT id FROM users WHERE role = 'mm_bot')
            OR maker_user_id IN (SELECT id FROM users WHERE role = 'mm_bot')
          )
        """
    )
    op.execute("UPDATE trades SET source = 'user' WHERE source = 'unknown'")
    op.execute(
        """
        UPDATE klines
        SET source = 'bootstrap_seed'
        WHERE EXISTS (
            SELECT 1
            FROM trades
            WHERE trades.market_id = klines.market_id
              AND trades.source = 'bootstrap_seed'
              AND trades.executed_at >= klines.open_time
              AND trades.executed_at <= klines.close_time
        )
        AND NOT EXISTS (
            SELECT 1
            FROM trades
            WHERE trades.market_id = klines.market_id
              AND trades.source <> 'bootstrap_seed'
              AND trades.executed_at >= klines.open_time
              AND trades.executed_at <= klines.close_time
        )
        """
    )


def downgrade() -> None:
    op.drop_index("idx_klines_market_interval_source_time", table_name="klines")
    op.drop_index("idx_trades_market_source_time", table_name="trades")
    op.drop_column("klines", "source")
    op.drop_column("trades", "source")
