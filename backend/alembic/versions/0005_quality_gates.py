"""Persist knowledge snapshots, index selection and evaluation token budgets."""

from alembic import op

from app.db.models import ActiveKnowledgeSnapshot, EvalTokenBudget, KnowledgeSnapshot, KnowledgeSnapshotChunk

revision = "0005_quality_gates"
down_revision = "0004_eval_center"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    for model in (EvalTokenBudget, KnowledgeSnapshot, KnowledgeSnapshotChunk, ActiveKnowledgeSnapshot):
        model.__table__.create(connection, checkfirst=True)


def downgrade():
    raise RuntimeError("Restore the pre-upgrade backup to preserve snapshots and budgets")
