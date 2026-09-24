"""Create Aiden's initial MySQL schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 按外键依赖顺序创建表：用户/目录先于订单，订单和会话先于其子表。
    op.create_table(
        # 用户是订单、会话和审核人等关系的根节点。
        "users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("role", sa.String(20), server_default=sa.text("'user'"), nullable=False),
        sa.Column("password_salt", sa.String(64), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("role IN ('user', 'staff')", name="ck_users_role_valid"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.UniqueConstraint("phone", name="uq_users_phone"),
    )
    # 商品目录与 SKU 分开保存；SKU 当前状态变化不影响后续订单快照。
    op.create_table(
        "products",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
    )
    op.create_index("ix_products_category_active", "products", ["category", "is_active"])
    # SKU 外键依赖商品；唯一编码用于查询具体规格。
    op.create_table(
        "product_skus",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=False),
        sa.Column("sku_code", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("specification", sa.JSON(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=False),
        sa.Column("stock_quantity", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("price >= 0", name="ck_product_skus_price_nonnegative"),
        sa.CheckConstraint("stock_quantity >= 0", name="ck_product_skus_stock_nonnegative"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_product_skus_product_id_products", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_product_skus"),
        sa.UniqueConstraint("sku_code", name="uq_product_skus_sku_code"),
    )
    op.create_index("ix_product_skus_product_active", "product_skus", ["product_id", "is_active"])
    # 订单保留用户、订单号和总额；复合唯一键供售后申请验证订单所有权。
    op.create_table(
        "orders",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("order_no", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), server_default=sa.text("'CNY'"), nullable=False),
        sa.Column("status", sa.String(32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("ordered_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("total_amount >= 0", name="ck_orders_total_amount_nonnegative"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_orders_user_id_users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("id", "user_id", name="uq_orders_id_user_id"),
        sa.UniqueConstraint("order_no", name="uq_orders_order_no"),
    )
    op.create_index("ix_orders_user_ordered", "orders", ["user_id", "ordered_at"])
    # 订单行存放下单时快照；可选 SKU 外键被置空时，快照仍保留历史信息。
    op.create_table(
        "order_items",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("sku_id", sa.String(36), nullable=True),
        sa.Column("product_name_snapshot", sa.String(200), nullable=False),
        sa.Column("sku_code_snapshot", sa.String(80), nullable=True),
        sa.Column("sku_name_snapshot", sa.String(200), nullable=True),
        sa.Column("specification_snapshot", sa.JSON(), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("line_total", sa.Numeric(12, 2), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        sa.CheckConstraint("unit_price >= 0", name="ck_order_items_unit_price_nonnegative"),
        sa.CheckConstraint("line_total >= 0", name="ck_order_items_line_total_nonnegative"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_order_items_order_id_orders", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sku_id"], ["product_skus.id"], name="fk_order_items_sku_id_product_skus", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_order_items"),
        sa.UniqueConstraint("id", "order_id", name="uq_order_items_id_order_id"),
    )
    op.create_index("ix_order_items_order", "order_items", ["order_id"])
    op.create_index("ix_order_items_sku", "order_items", ["sku_id"])
    # 一个订单可对应多个包裹；承运商和运单号组合用于防止重复登记。
    op.create_table(
        "shipments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("shipment_no", sa.String(64), nullable=True),
        sa.Column("carrier_code", sa.String(40), nullable=True),
        sa.Column("carrier_name", sa.String(100), nullable=True),
        sa.Column("tracking_no", sa.String(100), nullable=True),
        sa.Column("status", sa.String(32), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("shipped_at", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], name="fk_shipments_order_id_orders", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_shipments"),
        sa.UniqueConstraint("carrier_code", "tracking_no", name="uq_shipments_carrier_code_tracking_no"),
        sa.UniqueConstraint("shipment_no", name="uq_shipments_shipment_no"),
    )
    op.create_index("ix_shipments_order", "shipments", ["order_id"])
    # 每个包裹的轨迹按发生时间读取，删除包裹时其轨迹级联清理。
    op.create_table(
        "tracking_events",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("shipment_id", sa.String(36), nullable=False),
        sa.Column("event_code", sa.String(40), nullable=True),
        sa.Column("status", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_tracking_events_shipment_id_shipments", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_tracking_events"),
    )
    op.create_index("ix_tracking_events_shipment_occurred", "tracking_events", ["shipment_id", "occurred_at"])
    # 售后复合外键同时验证订单所有者和可选订单行所属订单。
    op.create_table(
        "after_sale_requests",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("request_no", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("order_id", sa.String(36), nullable=False),
        sa.Column("order_item_id", sa.String(36), nullable=True),
        sa.Column("request_type", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("requested_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("request_type IN ('return', 'refund', 'exchange')", name="ck_after_sale_requests_request_type_valid"),
        sa.CheckConstraint("status IN ('pending', 'approved', 'rejected', 'processing', 'completed', 'cancelled')", name="ck_after_sale_requests_status_valid"),
        sa.CheckConstraint("requested_amount IS NULL OR requested_amount >= 0", name="ck_after_sale_requests_requested_amount_nonnegative"),
        sa.ForeignKeyConstraint(["order_id", "user_id"], ["orders.id", "orders.user_id"], name="fk_after_sale_requests_order_id_user_id_orders", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_item_id", "order_id"], ["order_items.id", "order_items.order_id"], name="fk_after_sale_requests_order_item_id_order_id_order_items", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_after_sale_requests"),
        sa.UniqueConstraint("request_no", name="uq_after_sale_requests_request_no"),
    )
    op.create_index("ix_after_sale_requests_user_created", "after_sale_requests", ["user_id", "created_at"])
    op.create_index("ix_after_sale_requests_order", "after_sale_requests", ["order_id"])
    # 政策以键和版本区分，生效起止时间用于查询历史和当前版本。
    op.create_table(
        "policies",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("policy_key", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("effective_until", sa.DateTime(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="ck_policies_effective_interval_valid"),
        sa.PrimaryKeyConstraint("id", name="pk_policies"),
        sa.UniqueConstraint("policy_key", "version", name="uq_policies_policy_key_version"),
    )
    op.create_index("ix_policies_key_effective", "policies", ["policy_key", "is_active", "effective_from", "effective_until"])
    # FAQ 与政策版本分开管理，停用条目仍可保留审核和追溯信息。
    op.create_table(
        "faq",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("category", sa.String(100), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_faq"),
    )
    op.create_index("ix_faq_category_active", "faq", ["category", "is_active"])
    # 会话先于消息和工单创建；复合唯一键用于约束下游记录的所有者。
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("subject", sa.String(200), server_default=sa.text("'新建会话'"), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'bot'"), nullable=False),
        sa.Column("assigned_staff_id", sa.String(36), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("status IN ('bot', 'waiting', 'staff', 'closed')", name="ck_conversations_status_valid"),
        sa.ForeignKeyConstraint(["assigned_staff_id"], ["users.id"], name="fk_conversations_assigned_staff_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_conversations_user_id_users", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint("id", "user_id", name="uq_conversations_id_user_id"),
    )
    op.create_index("ix_conversations_user_updated", "conversations", ["user_id", "updated_at"])
    # 消息属于会话；索引包含时间和主键，保证消息历史可以稳定排序。
    op.create_table(
        "messages",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("sender_role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), server_default=sa.text("'complete'"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("sender_role IN ('user', 'assistant', 'staff', 'system')", name="ck_messages_sender_role_valid"),
        sa.CheckConstraint("status IN ('complete', 'streaming', 'error', 'stopped')", name="ck_messages_status_valid"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], name="fk_messages_conversation_id_conversations", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_messages"),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at", "id"])
    # 工单的会话与用户复合外键，阻止把一个用户的工单挂到他人的会话上。
    op.create_table(
        "tickets",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("ticket_no", sa.String(64), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("issue_type", sa.String(80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(24), server_default=sa.text("'open'"), nullable=False),
        sa.Column("assigned_staff_id", sa.String(36), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'resolved', 'closed')", name="ck_tickets_status_valid"),
        sa.ForeignKeyConstraint(["assigned_staff_id"], ["users.id"], name="fk_tickets_assigned_staff_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["conversation_id", "user_id"], ["conversations.id", "conversations.user_id"], name="fk_tickets_conversation_id_user_id_conversations", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_tickets"),
        sa.UniqueConstraint("ticket_no", name="uq_tickets_ticket_no"),
    )
    op.create_index("ix_tickets_user_status_created", "tickets", ["user_id", "status", "created_at"])
    # 未解决问题必须关联来源会话，并保留审核状态、审核人和审核记录。
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


def downgrade() -> None:
    # 按与 upgrade 相反的依赖顺序删除，先清理依赖表，再删除其父表。
    op.drop_table("unanswered_questions")
    op.drop_index("ix_tickets_user_status_created", table_name="tickets")
    op.drop_table("tickets")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_user_updated", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_faq_category_active", table_name="faq")
    op.drop_table("faq")
    op.drop_index("ix_policies_key_effective", table_name="policies")
    op.drop_table("policies")
    op.drop_index("ix_after_sale_requests_order", table_name="after_sale_requests")
    op.drop_index("ix_after_sale_requests_user_created", table_name="after_sale_requests")
    op.drop_table("after_sale_requests")
    op.drop_index("ix_tracking_events_shipment_occurred", table_name="tracking_events")
    op.drop_table("tracking_events")
    op.drop_index("ix_shipments_order", table_name="shipments")
    op.drop_table("shipments")
    op.drop_index("ix_order_items_sku", table_name="order_items")
    op.drop_index("ix_order_items_order", table_name="order_items")
    op.drop_table("order_items")
    op.drop_index("ix_orders_user_ordered", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_product_skus_product_active", table_name="product_skus")
    op.drop_table("product_skus")
    op.drop_index("ix_products_category_active", table_name="products")
    op.drop_table("products")
    op.drop_table("users")
