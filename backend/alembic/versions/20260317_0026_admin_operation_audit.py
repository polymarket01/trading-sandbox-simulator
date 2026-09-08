from __future__ import annotations

from alembic import op


revision = "20260317_0026"
down_revision = "20260317_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_operation_audits (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            operation_id VARCHAR(64) NOT NULL UNIQUE,
            created_at DATETIME NOT NULL,
            actor_user_id INTEGER,
            actor_username VARCHAR(64),
            domain VARCHAR(32) NOT NULL,
            operation_type VARCHAR(64) NOT NULL,
            target_type VARCHAR(32) NOT NULL,
            target_id VARCHAR(128),
            target_symbol VARCHAR(64),
            status VARCHAR(16) NOT NULL,
            summary VARCHAR(255) NOT NULL,
            result_json JSON,
            FOREIGN KEY(actor_user_id) REFERENCES users (id)
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_admin_ops_created ON admin_operation_audits (created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_admin_ops_domain_created ON admin_operation_audits (domain, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_admin_ops_type_created ON admin_operation_audits (operation_type, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_admin_ops_symbol_created ON admin_operation_audits (target_symbol, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_admin_ops_actor_created ON admin_operation_audits (actor_user_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_admin_ops_actor_created")
    op.execute("DROP INDEX IF EXISTS idx_admin_ops_symbol_created")
    op.execute("DROP INDEX IF EXISTS idx_admin_ops_type_created")
    op.execute("DROP INDEX IF EXISTS idx_admin_ops_domain_created")
    op.execute("DROP INDEX IF EXISTS idx_admin_ops_created")
    op.execute("DROP TABLE IF EXISTS admin_operation_audits")
