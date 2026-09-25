"""在线语义检索：用户问题 -> BGE-M3 dense 向量 -> Milvus Top-K -> MySQL 权威原文。

这一段是离线建库的镜像：离线把知识写成向量，在线把问题写成向量，两端共用
`app.services.rag.embedding_text.build_embedding_text` 的模板与同一个 `EmbeddingClient`。

三条硬约束：

1. **单路 dense 检索。** 不叠关键词召回、不做 MySQL LIKE 兜底、不做混合检索或重排。排序只有
   一个来源（Milvus 的 COSINE 相似度），出问题时能直接解释为什么是这几条。
2. **原文只信 MySQL。** Milvus 的 `content` 字段只是写库时留下的副本，可能落后于 MySQL；命中的
   块要按主键回到 `knowledge_chunks` 取 `category/questions/answer`，且只接受当前有效
   （`vectorized`）的块。Milvus 里找不到对应原文、状态不符或内容已失效的命中项一律丢弃。
3. **故障不等于空结果。** 向量服务或 Milvus 不可用时直接抛 `RetrievalError`，由工具层返回既有的
   通用错误语义；绝不静默退回关键词查询——那会让“检索服务坏了”表现成“没有这条知识”。

会话边界：Milvus 检索（含网络往返）期间不持有任何 MySQL Session，回表时才开短事务，与
`app.persistence.mysql.knowledge` 的约定一致。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config.rag import rag_settings
from app.persistence.mysql import knowledge as knowledge_repo
from app.persistence.mysql.knowledge import KnowledgeChunkRow
from app.persistence.milvus.knowledge_store import KnowledgeVectorStore

from .embedding import EmbeddingClient, EmbeddingError
from .embedding_text import build_query_embedding_text


class RetrievalError(RuntimeError):
    """在线检索依赖不可用或返回结果不符合约定。

    调用方应把它当作“检索暂时无法完成”处理，而不是“没有匹配知识”。
    """


@dataclass(frozen=True)
class SemanticKnowledgeHit:
    """一条通过校验的检索结果：原文来自 MySQL，相似度来自 Milvus。

    `score` 是 COSINE 相似度，`rank` 是 Milvus 的返回顺序（0 最相似）。两者都保留，是为了让
    工具层和排查过程能看出 Agent 拿到的是哪几条、排序依据是什么。
    """

    chunk: KnowledgeChunkRow
    score: float
    rank: int


def semantic_search(
    question: str,
    *,
    limit: int | None = None,
    candidate_limit: int | None = None,
    store: KnowledgeVectorStore | None = None,
    client: EmbeddingClient | None = None,
) -> list[SemanticKnowledgeHit]:
    """按语义相似度检索当前有效的知识，返回最多 `limit` 条。

    输入 `question` 是已经过工具层校验（非空、长度可控）的用户问题；空问题返回空列表而不调用
    外部服务。

    流程：拼 query 文本 -> BGE-M3 dense 向量 -> Milvus 取候选 -> MySQL 按主键回表并过滤 ->
    按相似度截取最终条数。候选数默认大于最终条数，用于补足被过滤掉的失效命中。

    `store` 与 `client` 只在测试或复用连接时显式传入；默认按当前配置构造，两端都读
    `app.config.rag` 的同一份 embedding/milvus 配置，因此 query 向量与库中向量必定同源。
    """
    normalized_question = question.strip()
    if not normalized_question:
        return []

    result_limit = limit if limit is not None else rag_settings.effective_online_result_limit
    if result_limit <= 0:
        return []
    candidate_pool = candidate_limit if candidate_limit is not None else rag_settings.online_candidate_pool
    candidate_pool = max(candidate_pool, result_limit)

    active_client = client or EmbeddingClient()
    try:
        vector = active_client.embed_documents([build_query_embedding_text(normalized_question)])[0]
    except EmbeddingError as exc:
        raise RetrievalError(f"生成问题向量失败：{exc}") from exc

    active_store = store or KnowledgeVectorStore(dimension=active_client.expected_dimension)
    try:
        matches = active_store.search(vector, limit=candidate_pool)
    except RetrievalError:
        raise
    except Exception as exc:  # Milvus 连接、集合缺失、索引未就绪等统一转为可读错误
        raise RetrievalError(f"Milvus 检索失败：{exc}") from exc
    if not matches:
        return []

    # Milvus 返回的候选可能重复（同一条知识被多次写入或索引未收敛），回表前先按主键去重，
    # 否则同一条知识会在结果里占掉多个位置，把真正不同的知识挤出 Top-K。
    ordered_ids = list(dict.fromkeys(match.chunk_id for match in matches))
    score_by_id = {match.chunk_id: match.score for match in matches}
    rank_by_id = {match.chunk_id: match.rank for match in matches}
    chunks = knowledge_repo.get_vectorized_chunks_by_ids(ordered_ids)

    hits = [
        SemanticKnowledgeHit(
            chunk=chunk,
            score=score_by_id[chunk.id],
            rank=rank_by_id[chunk.id],
        )
        for chunk in chunks
    ]
    # 回表顺序已经按候选顺序，这里显式再排一次，避免依赖数据库返回顺序。
    hits.sort(key=lambda hit: (-hit.score, hit.rank, hit.chunk.id))
    return hits[:result_limit]
