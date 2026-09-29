"""Retire the unused legacy unanswered_questions table.

Revision ID: 0011_remove_unanswered_questions
Revises: 0010_review_queue
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_remove_unanswered_questions"
down_revision = "0010_review_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 当前问题池和人工审核分别由 low_confidence_questions、review_queue 承担。
    # 旧表没有运行时写入入口；删除它会清除旧表的历史行，执行前需备份需要保留的数据。
    op.drop_table("unanswered_questions")


def downgrade() -> None:
    # 只能恢复表结构，无法恢复 upgrade 时删除的旧表数据。
    op.create_table(
        "unanswered_questions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("review_status", sa.String(24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("reviewer_id", sa.String(36), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="ck_unanswered_questions_confidence_range"),
        sa.CheckConstraint("review_status IN ('pending', 'reviewed', 'added_to_faq', 'dismissed')", name="ck_unanswered_questions_review_status_valid"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_unanswered_questions_conversation_id_conversations", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"], name="fk_unanswered_questions_reviewer_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_unanswered_questions"),
    )
    op.create_index("ix_unanswered_questions_status_created", "unanswered_questions", ["review_status", "created_at"])
    op.create_index("ix_unanswered_questions_conversation", "unanswered_questions", ["conversation_id"])
