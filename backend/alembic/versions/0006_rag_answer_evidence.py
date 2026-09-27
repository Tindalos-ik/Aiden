"""Persist answer citations and questions refused for insufficient knowledge.

Revision ID: 0006_rag_answer_evidence
Revises: 0005_conversation_mining
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0006_rag_answer_evidence"
down_revision = "0005_conversation_mining"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("citations", sa.JSON(), nullable=True))
    op.execute("UPDATE messages SET citations = JSON_ARRAY() WHERE citations IS NULL")
    op.alter_column("messages", "citations", existing_type=sa.JSON(), nullable=False)
    op.create_table(
        "low_confidence_questions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("original_question", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("entrypoint", sa.String(40), nullable=False),
        sa.Column("reason", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_low_confidence_questions"),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["conversations.id"],
            name="fk_low_confidence_questions_conversation_id_conversations",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_low_confidence_questions_conversation", "low_confidence_questions",
        ["conversation_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_low_confidence_questions_conversation", table_name="low_confidence_questions")
    op.drop_table("low_confidence_questions")
    op.drop_column("messages", "citations")
