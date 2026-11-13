"""Add cancelled task state and terminate legacy orphan ingestion.

Revision ID: 0017_cancelled_ingest_tasks
Revises: 0016_scale_safety_indexes
"""
from alembic import op

from library.db.bootstrap import _ensure_cancelled_tasks

revision = "0017_cancelled_ingest_tasks"
down_revision = "0016_scale_safety_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _ensure_cancelled_tasks(op.get_bind())


def downgrade() -> None:
    # Cancellation is historical state, not a failed parse. Retain its CHECK
    # and rows, like the other additive compatibility migrations.
    pass
