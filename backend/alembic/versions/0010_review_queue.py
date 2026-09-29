"""Keep per-issue retrieval evidence and reviewed FAQ promotion separate from old rows."""

from alembic import op
import sqlalchemy as sa


revision = "0010_review_queue"
down_revision = "0009_low_confidence_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("retrieval_snapshots", sa.JSON(), nullable=True))
    op.create_table(
        "review_queue",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("normalized_question", sa.Text(), nullable=False),
        sa.Column("example_answer", sa.Text(), nullable=False),
        sa.Column("occurrence_count", sa.Integer(), nullable=False),
        sa.Column("review_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("approved_answer", sa.Text()),
        sa.Column("category", sa.String(100)),
        sa.Column("review_note", sa.Text()),
        sa.Column("rejection_reason", sa.String(50)),
        sa.Column("reviewed_by_id", sa.String(36)),
        sa.Column("reviewed_at", sa.DateTime()),
        sa.Column("sync_token", sa.String(36)),
        sa.Column("sync_started_at", sa.DateTime()),
        sa.Column("faq_id", sa.String(36)),
        sa.Column("ingestion_status", sa.String(24), nullable=False, server_default="not_started"),
        sa.Column("ingestion_error", sa.String(255)),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("review_status IN ('pending', 'approved', 'rejected')", name="ck_review_queue_status_valid"),
        sa.CheckConstraint("ingestion_status IN ('not_started', 'pending', 'ready', 'manual_review', 'failed')", name="ck_review_queue_ingestion_valid"),
        sa.CheckConstraint("occurrence_count >= 1", name="ck_review_queue_occurrences_positive"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["faq_id"], ["faq.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("faq_id", name="uq_review_queue_faq_id"),
    )
    op.create_index("ix_review_queue_status_created", "review_queue", ["review_status", "created_at", "id"])
    op.add_column("low_confidence_questions", sa.Column("matched_review_id", sa.String(36)))
    op.add_column("low_confidence_questions", sa.Column("retrieval_snapshot", sa.JSON()))
    op.add_column("low_confidence_questions", sa.Column("retrieval_status", sa.String(24)))
    op.add_column("low_confidence_questions", sa.Column("retrieval_query", sa.String(500)))
    op.add_column("low_confidence_questions", sa.Column("processing_error", sa.String(255)))
    op.create_foreign_key("fk_low_confidence_questions_matched_review_id_review_queue", "low_confidence_questions", "review_queue", ["matched_review_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_low_confidence_unmatched", "low_confidence_questions", ["matched_review_id", "created_at", "id"])


def downgrade() -> None:
    op.drop_index("ix_low_confidence_unmatched", table_name="low_confidence_questions")
    op.drop_constraint("fk_low_confidence_questions_matched_review_id_review_queue", "low_confidence_questions", type_="foreignkey")
    for column in ("processing_error", "retrieval_query", "retrieval_status", "retrieval_snapshot", "matched_review_id"):
        op.drop_column("low_confidence_questions", column)
    op.drop_index("ix_review_queue_status_created", table_name="review_queue")
    op.drop_table("review_queue")
    op.drop_column("messages", "retrieval_snapshots")
