"""离线建库（RAG）相关配置。

与 `app.config.settings` 分开，是因为这里的参数只服务于“读取知识 -> 切分 -> 向量化 ->
写入 Milvus”的离线流程，在线对话链路不读这些值。所有外部地址和凭据都从进程环境
（本地 `backend/.env`）读取，示例文件里只放占位值。

切分参数在这里集中定义，长度一律以 **BGE-M3 的 token** 为准，换算方式见
`app.services.rag.length`：安装了 `tokenizers` 且配置了本地 tokenizer.json 时精确计数，
否则退回按字符估算，并把估算比例和不确定度显式计入预算。
"""

from __future__ import annotations

import os
from pathlib import Path

# 导入 settings 模块即触发 backend/.env 的加载，保证命令行脚本和 API 进程看到同一份配置。
from . import settings as _settings  # noqa: F401

# backend 目录，用于把相对的知识目录配置解析成绝对路径。
BACKEND_DIR = Path(__file__).resolve().parents[2]


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；为空时使用默认值，非法值直接报错而不是静默取默认。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量；用于长度估算比例这类可调参数。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


class RagSettings:
    """离线建库的运行时参数。

    `chunk_budget_tokens` 是实际用于装正文的 token 预算，等于模型上限减去长度计量的
    不确定度和安全余量：估算模式下必须留出余量，否则真实 token 数可能越过模型上限。
    """

    # --- BGE-M3 dense 向量服务（OpenAI 兼容 /v1/embeddings 端点）---
    # 服务可以是本地私有化部署，也可以是公司内网网关；真实地址与密钥只写在 backend/.env。
    embedding_base_url: str = os.getenv("EMBEDDING_BASE_URL", "http://127.0.0.1:8001/v1").strip()
    embedding_api_key: str = os.getenv("EMBEDDING_API_KEY", "").strip()
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3").strip()
    # BGE-M3 的 dense 向量维度和最长序列长度；维度会在首次响应后与实际输出校验。
    embedding_dimension: int = _env_int("EMBEDDING_DIMENSION", 1024)
    embedding_max_seq_tokens: int = _env_int("EMBEDDING_MAX_SEQ_TOKENS", 8192)
    # 只用于精确长度计量，不参与向量计算；缺失时退回字符估算。
    embedding_tokenizer_path: str = os.getenv("EMBEDDING_TOKENIZER_PATH", "").strip()
    embedding_request_timeout: float = _env_float("EMBEDDING_REQUEST_TIMEOUT", 120.0)
    # 单次请求送几条文本；批大小只影响吞吐，不影响向量结果。
    embedding_batch_size: int = _env_int("EMBEDDING_BATCH_SIZE", 16)

    # --- Milvus knowledge 集合 ---
    milvus_uri: str = os.getenv("MILVUS_URI", "http://127.0.0.1:19530").strip()
    milvus_token: str = os.getenv("MILVUS_TOKEN", "").strip()
    milvus_collection: str = os.getenv("MILVUS_KNOWLEDGE_COLLECTION", "knowledge").strip()
    milvus_insert_batch_size: int = _env_int("MILVUS_INSERT_BATCH_SIZE", 64)

    # --- 切分参数 ---
    # 单块上限。注意：真正的上限还要受 EMBEDDING_MAX_SEQ_TOKENS 约束（取两者较小值），
    # 否则块会超过模型能编码的长度，服务侧只能自行截断，切分预算就失去意义。
    max_chunk_tokens: int = _env_int("RAG_MAX_CHUNK_TOKENS", 8192)
    # 单句上限：超过它的句子视为异常长句，整块标记待人工处理，不做静默截断。
    max_sentence_tokens: int = _env_int("RAG_MAX_SENTENCE_TOKENS", 2048)
    # 相邻块的重叠量，只从上一块尾部按完整句子回退取得，不会切断句子。
    overlap_tokens: int = _env_int("RAG_OVERLAP_TOKENS", 80)
    # 长度计量的不确定度：估算模式下按字符换算可能低估，先把这部分从预算里扣掉。
    measurement_uncertainty_tokens: int = _env_int("RAG_MEASUREMENT_UNCERTAINTY_TOKENS", 256)
    # 额外安全余量，避免模型侧分词与本地计量存在细微差异时越界。
    safety_margin_tokens: int = _env_int("RAG_SAFETY_MARGIN_TOKENS", 64)
    # 章节标题递归下钻的最大层数，超过后按句子打包，防止空标题层级导致无限递归。
    max_heading_depth: int = _env_int("RAG_MAX_HEADING_DEPTH", 6)
    # 表格分块时每块最多保留的数据行数（不含复制的表头），保证每块能独立理解且不超长。
    table_rows_per_chunk: int = _env_int("RAG_TABLE_ROWS_PER_CHUNK", 20)
    # 字符估算模式下 1 个 token 大约对应多少字符；仅用于长度估算，不参与向量化。
    estimator_chars_per_token: float = _env_float("RAG_ESTIMATOR_CHARS_PER_TOKEN", 1.1)
    # 设为 true 时，缺少 tokenizers 或 tokenizer.json 会直接报错，而不再退回字符估算。
    require_exact_tokenizer: bool = os.getenv("RAG_REQUIRE_EXACT_TOKENIZER", "false").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    # 默认知识目录；相对路径按 backend 目录解析。导入命令的 --directory 参数优先。
    knowledge_dir: str = os.getenv("RAG_KNOWLEDGE_DIR", "knowledge").strip() or "knowledge"

    @property
    def knowledge_directory(self) -> Path:
        """默认知识目录的绝对路径；相对配置按 backend 目录解析。"""
        configured = Path(self.knowledge_dir)
        return configured if configured.is_absolute() else (BACKEND_DIR / configured)

    @property
    def effective_max_chunk_tokens(self) -> int:
        """单块 token 上限：取配置上限与模型可编码长度中较小的一个。

        BGE-M3 支持 8192，但许多 BGE 模型（如 bge-small-zh-v1.5）只有 512。切分预算如果按
        RAG_MAX_CHUNK_TOKENS 单独决定，块就会超过模型长度，推理服务只能截断，等于静默丢内容。
        因此这里以模型能力为硬上限。
        """
        return max(min(self.max_chunk_tokens, self.embedding_max_seq_tokens), 1)

    @property
    def effective_max_sentence_tokens(self) -> int:
        """生效的单句上限。

        不能大于配置值，也不能超过模型长度的二分之一：超过上限的单位会独立成块（便于人工
        拆分），如果这个上限接近模型长度，独立成块后就没有任何余量可言。对小模型（512）来说
        这个约束会自动收紧，不需要人工再调 RAG_MAX_SENTENCE_TOKENS。
        """
        half_ceiling = max(self.effective_max_chunk_tokens // 2, 1)
        return max(min(self.max_sentence_tokens, half_ceiling), 1)

    @property
    def chunk_budget_tokens(self) -> int:
        """正文可用的 token 预算。

        先在模型可编码长度内扣掉长度计量的不确定度与安全余量；再保证“预算 + 一个独立成块的
        超长单位”也不越界。这里的上限是**模型长度**而不是单句上限，所以不会出现负值被兜底成
        极小预算的情况——单句上限本身已经限制在模型长度的一半以内。
        """
        ceiling = self.effective_max_chunk_tokens
        after_margin = ceiling - self.measurement_uncertainty_tokens - self.safety_margin_tokens
        bounded = ceiling - self.effective_max_sentence_tokens
        return max(min(after_margin, bounded), 1)

    @property
    def embedding_service_configured(self) -> bool:
        """是否已经配置向量服务地址；未配置时导入脚本会给出明确提示。"""
        return bool(self.embedding_base_url)


rag_settings = RagSettings()
