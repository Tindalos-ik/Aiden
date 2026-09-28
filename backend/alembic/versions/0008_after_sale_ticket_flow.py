"""Add guarded after-sale submissions and ticket processing metadata.

Revision ID: 0008_after_sale_ticket_flow
Revises: 0007_refund_dialogue_state
"""

from alembic import op
import sqlalchemy as sa

revision = "0008_after_sale_ticket_flow"
down_revision = "0007_refund_dialogue_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_after_sale_requests_status_valid", "after_sale_requests", type_="check")
    op.create_check_constraint("ck_after_sale_requests_status_valid", "after_sale_requests", "status IN ('pending', 'approved', 'rejected', 'processing', 'awaiting_external_refund', 'completed', 'cancelled')")
    op.add_column("after_sale_requests", sa.Column("submission_key", sa.String(100)))
    op.add_column("after_sale_requests", sa.Column("policy_reference", sa.String(255)))
    op.add_column("after_sale_requests", sa.Column("reviewed_by_id", sa.String(36)))
    op.add_column("after_sale_requests", sa.Column("reviewed_at", sa.DateTime()))
    op.add_column("after_sale_requests", sa.Column("processing_at", sa.DateTime()))
    op.add_column("after_sale_requests", sa.Column("processing_by_id", sa.String(36)))
    op.add_column("after_sale_requests", sa.Column("resolved_by_id", sa.String(36)))
    op.add_column("after_sale_requests", sa.Column("decision_note", sa.Text()))
    op.create_unique_constraint("uq_after_sale_submission", "after_sale_requests", ["user_id", "submission_key"])
    op.create_unique_constraint("uq_after_sale_id_user", "after_sale_requests", ["id", "user_id"])
    op.create_foreign_key("fk_after_sale_reviewed_by", "after_sale_requests", "users", ["reviewed_by_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_after_sale_processing_by", "after_sale_requests", "users", ["processing_by_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("fk_after_sale_resolved_by", "after_sale_requests", "users", ["resolved_by_id"], ["id"], ondelete="SET NULL")
    op.add_column("tickets", sa.Column("after_sale_request_id", sa.String(36)))
    op.add_column("tickets", sa.Column("accepted_at", sa.DateTime()))
    op.add_column("tickets", sa.Column("closed_at", sa.DateTime()))
    op.add_column("tickets", sa.Column("resolution_note", sa.Text()))
    op.add_column("tickets", sa.Column("closed_by_id", sa.String(36)))
    op.create_foreign_key("fk_tickets_after_sale_owner", "tickets", "after_sale_requests", ["after_sale_request_id", "user_id"], ["id", "user_id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_tickets_closed_by", "tickets", "users", ["closed_by_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_tickets_closed_by", "tickets", type_="foreignkey")
    op.drop_constraint("fk_tickets_after_sale_owner", "tickets", type_="foreignkey")
    for column in ("closed_by_id", "resolution_note", "closed_at", "accepted_at", "after_sale_request_id"):
        op.drop_column("tickets", column)
    op.drop_constraint("fk_after_sale_reviewed_by", "after_sale_requests", type_="foreignkey")
    op.drop_constraint("fk_after_sale_processing_by", "after_sale_requests", type_="foreignkey")
    op.drop_constraint("fk_after_sale_resolved_by", "after_sale_requests", type_="foreignkey")
    op.drop_constraint("uq_after_sale_id_user", "after_sale_requests", type_="unique")
    op.drop_constraint("uq_after_sale_submission", "after_sale_requests", type_="unique")
    for column in ("decision_note", "resolved_by_id", "processing_by_id", "processing_at", "reviewed_at", "reviewed_by_id", "policy_reference", "submission_key"):
        op.drop_column("after_sale_requests", column)
    op.drop_constraint("ck_after_sale_requests_status_valid", "after_sale_requests", type_="check")
    op.create_check_constraint("ck_after_sale_requests_status_valid", "after_sale_requests", "status IN ('pending', 'approved', 'rejected', 'processing', 'completed', 'cancelled')")
