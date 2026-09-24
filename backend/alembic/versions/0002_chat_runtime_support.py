"""增加登录会话表与对话运行所需字段。

本迁移只追加到 0001_initial 后，不回写初始迁移，便于已有数据库按版本升级。
client_message_id 与消息 UUID 主键分开，last_message_preview 则补齐会话列表字段。
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_chat_runtime"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 旧会话没有预览列，使用空字符串默认值平滑兼容已有记录。
    op.add_column(
        "conversations",
        sa.Column("last_message_preview", sa.String(120), server_default=sa.text("''"), nullable=False),
    )
    op.add_column("messages", sa.Column("client_message_id", sa.String(100), nullable=True))
    # 同一会话中，同一发送角色的客户端重试键不可重复；NULL 留给历史消息和系统消息。
    op.create_unique_constraint(
        "uq_messages_conversation_id_sender_role_client_message_id",
        "messages",
        ["conversation_id", "sender_role", "client_message_id"],
    )
    # Cookie 仍只发放随机令牌，数据库仅存令牌哈希、归属用户和 UTC 过期时间。
    op.create_table(
        "login_sessions",
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_login_sessions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("token_hash", name="pk_login_sessions"),
    )
    op.create_index("ix_login_sessions_expires_at", "login_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_login_sessions_expires_at", table_name="login_sessions")
    op.drop_table("login_sessions")
    op.drop_constraint(
        "uq_messages_conversation_id_sender_role_client_message_id",
        "messages",
        type_="unique",
    )
    op.drop_column("messages", "client_message_id")
    op.drop_column("conversations", "last_message_preview")
