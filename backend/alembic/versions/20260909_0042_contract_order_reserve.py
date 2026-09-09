"""Explicit remaining-order reserve; NULL preserves legacy full-margin facts."""
import sqlalchemy as sa
from alembic import op
revision = "20260909_0042"
down_revision = "20260317_0041"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("orders", sa.Column("reserved_margin", sa.Numeric(36, 18), nullable=True))

def downgrade():
    op.drop_column("orders", "reserved_margin")
