"""Add the conversation-mining staging tables for knowledge extraction from history.

Two tables are added. `conversation_mining_batches` records each extraction window together
with its progress state, so an interrupted run can resume from the batches that were not
extracted yet and a repeated run can recognise the same window by its signature instead of
calling the model again. `conversation_qa_candidates` stages the question/answer pairs that
the model produced; only candidates that pass structural, grounding and de-duplication
checks are promoted into `knowledge_chunks`, and the rejected ones stay here with a reason
so they can be reviewed later.

The migration also widens `knowledge_chunks.content_type` so mined knowledge can be marked
with the human-readable type 对话挖掘问答 (24 bytes in UTF-8, longer than the previous
VARCHAR(16) limit).

Revision ID: 0005_conversation_mining
Revises: 0004_knowledge_chunks
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0005_conversation_mining"
down_revision = "0004_knowledge_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_mining_batches",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        # 窗口签名对“会话 id + 窗口内消息 id 与发送角色序列”取哈希，是重复运行的幂等键。
        sa.Column("batch_signature", sa.String(64), nullable=False),
        # run_id 在批次被认领、真正要调模型时才写入，因此待抽取批次为 NULL。
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("claimed_by", sa.String(36), nullable=True),
        # 状态机：pending 待抽取、extracting 已被某次运行认领、extracted 已抽取待整体去重、
        # ready 本运行批次抽取完毕可去重、promoted 已入库、superseded 被更细的新批次取代、
        # skipped 已处置但没产生知识（候选全为重复或冲突、或单轮超长）、failed 抽取失败可重跑。
        sa.Column("status", sa.String(20), server_default=sa.text("'pending'"), nullable=False),
        # 消息时间使用 DATETIME(6)，与 messages.created_at 的微秒精度保持一致，
        # 续跑锚点比较才不会被截断到秒而重复或漏读同一秒内的消息。
        sa.Column("first_message_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("last_message_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False),
        sa.Column("turn_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("candidate_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("extracted_at", sa.DateTime(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'extracting', 'extracted', 'ready', 'promoted', "
            "'superseded', 'skipped', 'failed')",
            name="ck_conversation_mining_batches_batch_status",
        ),
        sa.CheckConstraint(
            "turn_count > 0", name="ck_conversation_mining_batches_turn_count_positive"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_mining_batches"),
        sa.UniqueConstraint(
            "batch_signature", name="uq_conversation_mining_batches_batch_signature"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_mining_batches_conversation_id_conversations",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_conversation_mining_batches_conversation",
        "conversation_mining_batches",
        ["conversation_id", "first_message_at"],
    )
    op.create_index(
        "ix_conversation_mining_batches_status",
        "conversation_mining_batches",
        ["status", "conversation_id"],
    )
    op.create_index(
        "ix_conversation_mining_batches_run", "conversation_mining_batches", ["run_id", "status"]
    )

    op.create_table(
        "conversation_qa_candidates",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("batch_id", sa.String(36), nullable=False),
        # 冗余保存会话标识，便于不联表按会话统计与排查。
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("candidate_key", sa.String(64), nullable=False),
        # 暂存的是脱敏后的真实问法与答案：个人与交易标识在调用模型前就已处理。
        sa.Column("question", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("answer", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("category", sa.String(255), nullable=False),
        sa.Column("source_user_message_id", sa.String(36), nullable=False),
        sa.Column("source_answer_message_id", sa.String(36), nullable=False),
        sa.Column("evidence", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'staged'"), nullable=False),
        sa.Column("rejection_reason", sa.String(500), nullable=True),
        sa.Column("answer_fingerprint", sa.String(64), nullable=False),
        sa.Column("deduped_at", sa.DateTime(), nullable=True),
        sa.Column("promoted_chunk_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            "status IN ('staged', 'promoted', 'rejected')",
            name="ck_conversation_qa_candidates_candidate_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_qa_candidates"),
        sa.UniqueConstraint("candidate_key", name="uq_conversation_qa_candidates_candidate_key"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["conversation_mining_batches.id"],
            # 命名规则展开后是 66 字符，超过 MySQL 8.4 的 64 字符标识符上限；这里显式给出
            # 等价的 63 字符名称，避免迁移在有唯一键的表上直接失败。
            name="fk_conversation_qa_candidates_batch_id_conversation_mining_b",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_conversation_qa_candidates_batch", "conversation_qa_candidates", ["batch_id", "status"]
    )
    op.create_index(
        "ix_conversation_qa_candidates_conversation",
        "conversation_qa_candidates",
        ["conversation_id", "status"],
    )
    op.create_index(
        "ix_conversation_qa_candidates_fingerprint",
        "conversation_qa_candidates",
        ["answer_fingerprint", "status"],
    )

    # 对话挖掘出的知识用可读的内容类型标记；原 VARCHAR(16) 装不下中文字面值，这里加宽到 64。
    op.alter_column(
        "knowledge_chunks",
        "content_type",
        existing_type=sa.String(16),
        type_=sa.String(64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "knowledge_chunks",
        "content_type",
        existing_type=sa.String(64),
        type_=sa.String(16),
        existing_nullable=False,
    )
    op.drop_index("ix_conversation_qa_candidates_fingerprint", table_name="conversation_qa_candidates")
    op.drop_index("ix_conversation_qa_candidates_conversation", table_name="conversation_qa_candidates")
    op.drop_index("ix_conversation_qa_candidates_batch", table_name="conversation_qa_candidates")
    op.drop_table("conversation_qa_candidates")
    op.drop_index("ix_conversation_mining_batches_run", table_name="conversation_mining_batches")
    op.drop_index("ix_conversation_mining_batches_status", table_name="conversation_mining_batches")
    op.drop_index("ix_conversation_mining_batches_conversation", table_name="conversation_mining_batches")
    op.drop_table("conversation_mining_batches")
