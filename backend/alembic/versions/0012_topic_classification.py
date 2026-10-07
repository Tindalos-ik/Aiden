"""新增独立的主题模型预测、人工最终标签与审计记录。

Revision ID: 0012_topic_classification
Revises: 0011_remove_unanswered_questions
"""
from alembic import op
import sqlalchemy as sa


revision = "0012_topic_classification"
down_revision = "0011_remove_unanswered_questions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "topic_classification_predictions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("predicted_labels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("model_version", sa.String(255), nullable=False),
        sa.Column("taxonomy_version", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint("status IN ('predicted', 'uncertain')", name=op.f("ck_topic_classification_predictions_status_valid")),
        sa.ForeignKeyConstraint(["source_id"], ["low_confidence_questions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "input_hash", "model_version", name="uq_topic_prediction_source_input_model"),
    )
    op.create_index(
        "ix_topic_prediction_model_source", "topic_classification_predictions", ["model_version", "source_id"]
    )
    op.create_table(
        "topic_classification_human_reviews",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("source_id", sa.String(36), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewed_by_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.CheckConstraint(
            "status IN ('confirmed', 'uncertain', 'insufficient_context')",
            name=op.f("ck_topic_classification_human_reviews_status_valid"),
        ),
        sa.ForeignKeyConstraint(["source_id"], ["low_confidence_questions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "input_hash", name="uq_topic_human_source_input"),
    )
    op.create_index("ix_topic_human_status", "topic_classification_human_reviews", ["status", "updated_at"])
    op.create_table(
        "topic_classification_human_audits",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("human_review_id", sa.String(36), nullable=False),
        sa.Column("staff_id", sa.String(36), nullable=False),
        sa.Column("previous_labels", sa.JSON(), nullable=True),
        sa.Column("previous_status", sa.String(32), nullable=True),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
        sa.ForeignKeyConstraint(
            ["human_review_id"], ["topic_classification_human_reviews.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["staff_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_topic_human_audit_review_time", "topic_classification_human_audits", ["human_review_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_topic_human_audit_review_time", table_name="topic_classification_human_audits")
    op.drop_table("topic_classification_human_audits")
    op.drop_index("ix_topic_human_status", table_name="topic_classification_human_reviews")
    op.drop_table("topic_classification_human_reviews")
    op.drop_index("ix_topic_prediction_model_source", table_name="topic_classification_predictions")
    op.drop_table("topic_classification_predictions")
