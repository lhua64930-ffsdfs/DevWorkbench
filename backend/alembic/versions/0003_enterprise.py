"""Expiring identities and cross-process admission control."""
from alembic import op
import sqlalchemy as sa

revision = "0003_enterprise"
down_revision = "0002_ci_sandbox"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {column["name"] for column in sa.inspect(connection).get_columns("users")}
    if "token_expires_at" not in columns:
        op.add_column("users", sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True))
    if "disabled" not in columns:
        op.add_column("users", sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    # Old tokens require explicit rotation in production; no invented issue date.
    from app.db.models import ResourceLease, RequestQuota
    ResourceLease.__table__.create(connection, checkfirst=True)
    RequestQuota.__table__.create(connection, checkfirst=True)


def downgrade():
    raise RuntimeError("Restore the pre-upgrade database backup to downgrade identities safely")
