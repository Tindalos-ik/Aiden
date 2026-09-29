"""Link low-confidence records to their messages and persist immutable feedback.

Existing low-confidence rows keep nullable source IDs instead of being backfilled by
an ambiguous historical message-order heuristic.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_low_confidence_feedback"
down_revision = "0008_after_sale_ticket_flow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("feedback", sa.String(20), nullable=True))
    op.create_check_constraint(
        "ck_messages_feedback_valid", "messages",
        "feedback IS NULL OR feedback IN ('satisfied', 'unsatisfied')",
    )
    op.add_column("low_confidence_questions", sa.Column("source_user_message_id", sa.String(36)))
    op.add_column("low_confidence_questions", sa.Column("source_assistant_message_id", sa.String(36)))
    op.create_foreign_key(
        "fk_low_confidence_source_user_message", "low_confidence_questions", "messages",
        ["source_user_message_id"], ["id"], ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_low_confidence_source_assistant_message", "low_confidence_questions", "messages",
        ["source_assistant_message_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index(
        "ix_low_confidence_source_assistant", "low_confidence_questions",
        ["source_assistant_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_low_confidence_source_assistant", table_name="low_confidence_questions")
    op.drop_constraint("fk_low_confidence_source_assistant_message", "low_confidence_questions", type_="foreignkey")
    op.drop_constraint("fk_low_confidence_source_user_message", "low_confidence_questions", type_="foreignkey")
    op.drop_column("low_confidence_questions", "source_assistant_message_id")
    op.drop_column("low_confidence_questions", "source_user_message_id")
    op.drop_constraint("ck_messages_feedback_valid", "messages", type_="check")
    op.drop_column("messages", "feedback")
