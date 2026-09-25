"""BGE-M3 dense 向量客户端。

按 OpenAI 兼容的 `/v1/embeddings` 协议访问向量服务（可以是本地私有化部署的服务，也可以是
内网网关），调用方只拿到 Python 列表，不持有任何数据库会话。

维度以 **实际模型输出** 为准：构造时带上配置中的期望维度，首次响应如果与期望不一致就直接
报错，而不是把不匹配的向量写进 Milvus —— Milvus 集合一旦按错误维度建好，后续写入会持续
失败，早失败比晚失败便宜得多。
"""

from __future__ import annotations

from openai import OpenAI

from app.config.rag import rag_settings


class EmbeddingError(RuntimeError):
    """向量服务不可用或返回结果不符合约定。"""


class DimensionMismatchError(EmbeddingError):
    """实际向量维度与配置不一致；需要先确认模型与集合维度再重跑。"""


class EmbeddingClient:
    """BGE-M3 dense 向量客户端。

    批大小由 `EMBEDDING_BATCH_SIZE` 控制：只影响请求次数，不影响单条向量结果，因此重跑
    补齐时时可以安全地改变它。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        expected_dimension: int | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._base_url = (base_url or rag_settings.embedding_base_url).rstrip("/")
        self._model = model or rag_settings.embedding_model
        self._expected_dimension = expected_dimension or rag_settings.embedding_dimension
        self._batch_size = batch_size or rag_settings.embedding_batch_size
        if not self._base_url:
            raise EmbeddingError(
                "未配置 EMBEDDING_BASE_URL，请指向 BGE-M3 的 OpenAI 兼容 /v1/embeddings 服务。"
            )
        # 服务未开启鉴权时给一个占位 key，SDK 要求该字段非空。
        self._client = OpenAI(
            base_url=self._base_url,
            api_key=api_key or rag_settings.embedding_api_key or "not-required",
            timeout=rag_settings.embedding_request_timeout,
        )

    @property
    def model(self) -> str:
        """当前使用的向量模型名，用于交付说明和日志。"""
        return self._model

    @property
    def expected_dimension(self) -> int:
        """配置中声明的向量维度；实际维度会在首次响应时校验。"""
        return self._expected_dimension

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量计算 dense 向量。

        输入文本为 `build_embedding_text` 的产物；返回顺序与输入严格一致，长度也必须一致。
        任何维度不符或条数不符都会抛错，避免“错位回填”污染向量库。
        """
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            try:
                response = self._client.embeddings.create(model=self._model, input=batch)
            except Exception as exc:  # 网络错误、鉴权失败或服务端异常统一转为可读错误
                raise EmbeddingError(f"调用向量服务失败：{exc}") from exc
            if len(response.data) != len(batch):
                raise EmbeddingError(
                    f"向量服务返回条数与请求不一致：请求 {len(batch)} 条，返回 {len(response.data)} 条。"
                )
            for item in sorted(response.data, key=lambda entry: entry.index):
                vector = list(item.embedding)
                if len(vector) != self._expected_dimension:
                    raise DimensionMismatchError(
                        f"向量维度与配置不一致：期望 {self._expected_dimension}，实际 {len(vector)}。"
                        "请先确认 EMBEDDING_MODEL 与 EMBEDDING_DIMENSION，再重建 Milvus 集合。"
                    )
                vectors.append(vector)
        return vectors
