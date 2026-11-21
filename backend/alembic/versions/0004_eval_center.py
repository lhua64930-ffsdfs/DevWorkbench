"""Add immutable evaluation datasets and case checkpoints."""

from alembic import op

from app.db.models import EvalCaseCheckpoint, EvalDatasetVersion

revision = "0004_eval_center"
down_revision = "0003_enterprise"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    EvalDatasetVersion.__table__.create(connection, checkfirst=True)
    EvalCaseCheckpoint.__table__.create(connection, checkfirst=True)


def downgrade():
    raise RuntimeError("Restore the pre-upgrade backup to preserve evaluation history")
