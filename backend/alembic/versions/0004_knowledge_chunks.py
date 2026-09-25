"""Add the knowledge_chunks table for offline RAG indexing.

MySQL is the authoritative store for knowledge text: every chunk keeps its original
category, questions and answer together with the metadata that must never be embedded
(section path, content type, key-clause flag, neighbour pointers). The vector columns
hold the Milvus synchronisation state so that an interrupted build can resume without
creating duplicate vectors.

Revision ID: 0004_knowledge_chunks
Revises: 0003_message_timestamp_precision
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0004_knowledge_chunks"
down_revision = "0003_message_timestamp_precision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(36), nullable=False),
        # chunk_key 是“同一来源、同一序号、同一内容”的稳定指纹，导入时以它做 MySQL 侧幂等 upsert；
        # id 同时也是 Milvus 的主键，因此重复执行只会覆盖同一条向量，不会产生重复。
        sa.Column("chunk_key", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("source_path", sa.String(512), nullable=True),
        sa.Column("source_title", sa.String(255), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(255), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("answer", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("embedding_text", mysql.MEDIUMTEXT(), nullable=False),
        sa.Column("embedding_fingerprint", sa.String(64), nullable=False),
        sa.Column("section_path", sa.String(512), nullable=False),
        sa.Column("content_type", sa.String(16), nullable=False),
        sa.Column("is_key_clause", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("prev_chunk_id", sa.String(36), nullable=True),
        sa.Column("next_chunk_id", sa.String(36), nullable=True),
        # 向量状态机：pending（原文已落库、待向量化）、vectorized（Milvus 已写入并回填）、
        # need_manual_review（单句超长，为避免静默截断不向量化）、superseded（内容已更新，旧向量作废）。
        sa.Column("vector_status", sa.String(24), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("vector_id", sa.String(128), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("vectorized_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            "vector_status IN ('pending', 'vectorized', 'need_manual_review', 'superseded')",
            name="ck_knowledge_chunks_vector_status_valid",
        ),
        sa.CheckConstraint(
            "source_type IN ('markdown', 'faq', 'conversation')",
            name="ck_knowledge_chunks_source_type_valid",
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_knowledge_chunks_chunk_index_nonnegative"),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_chunks"),
        sa.UniqueConstraint("chunk_key", name="uq_knowledge_chunks_chunk_key"),
    )
    op.create_index("ix_knowledge_chunks_source_index", "knowledge_chunks", ["source_type", "source_id", "chunk_index"])
    op.create_index("ix_knowledge_chunks_vector_status", "knowledge_chunks", ["vector_status", "id"])
    op.create_index("ix_knowledge_chunks_category_active", "knowledge_chunks", ["category", "vector_status"])


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_category_active", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_vector_status", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_source_index", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
