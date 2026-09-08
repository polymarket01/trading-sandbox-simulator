from __future__ import annotations

from alembic import op


revision = "20260317_0019"
down_revision = "20260317_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("contract_positions") as batch_op:
        batch_op.drop_constraint("uq_contract_position_user_market", type_="unique")
        batch_op.create_unique_constraint(
            "uq_contract_position_user_market_side",
            ["user_id", "market_id", "side"],
        )


def downgrade() -> None:
    with op.batch_alter_table("contract_positions") as batch_op:
        batch_op.drop_constraint("uq_contract_position_user_market_side", type_="unique")
        batch_op.create_unique_constraint(
            "uq_contract_position_user_market",
            ["user_id", "market_id"],
        )
