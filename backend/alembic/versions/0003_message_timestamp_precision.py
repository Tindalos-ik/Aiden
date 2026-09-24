"""Preserve message ordering within a second.

MySQL DATETIME without fractional precision truncated the one-microsecond offset
between user messages and their assistant placeholders, leaving UUIDs to decide
their display order. Store message timestamps at microsecond precision and repair
existing user/assistant pairs using their shared client message ID.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0003_message_timestamp_precision"
down_revision = "0002_chat_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "created_at",
        existing_type=mysql.DATETIME(),
        type_=mysql.DATETIME(fsp=6),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP"),
        server_default=sa.text("CURRENT_TIMESTAMP(6)"),
    )
    op.execute(
        sa.text(
            """
            UPDATE messages AS assistant_message
            JOIN messages AS user_message
              ON user_message.conversation_id = assistant_message.conversation_id
             AND user_message.client_message_id = assistant_message.client_message_id
             AND user_message.sender_role = 'user'
             AND assistant_message.sender_role = 'assistant'
            SET assistant_message.created_at = user_message.created_at + INTERVAL 1 MICROSECOND
            WHERE assistant_message.client_message_id IS NOT NULL
              AND assistant_message.created_at = user_message.created_at
            """
        )
    )


def downgrade() -> None:
    op.alter_column(
        "messages",
        "created_at",
        existing_type=mysql.DATETIME(fsp=6),
        type_=mysql.DATETIME(),
        existing_nullable=False,
        existing_server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
