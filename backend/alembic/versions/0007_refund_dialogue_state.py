"""Persist verified refund dialogue stage on assistant messages.

Revision ID: 0007_refund_dialogue_state
Revises: 0006_rag_answer_evidence
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0007_refund_dialogue_state"
down_revision = "0006_rag_answer_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("workflow_state", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "workflow_state")
