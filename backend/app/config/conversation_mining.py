"""历史对话挖掘（RAG 知识抽取）的配置。

与 `app.config.rag` 分开：`rag` 管的是“已有知识怎么切分与向量化”，这里管的是“从 conversations /
messages 里把知识抽出来”——批次规模、抽取模型参数和定时周期。两者都只在离线任务里使用，
在线对话链路不读这些值。

LLM 凭据默认复用 `OPENAI_*`（与在线 Agent 同一套），因为多数部署里就是同一个网关；但抽取是
独立的离线调用，允许用 `MINING_LLM_*` 覆盖，避免离线批量抽取的模型与温度影响在线客服回答。
"""

from __future__ import annotations

import os

# 导入 settings 模块即触发 backend/.env 的加载，保证命令行脚本与 API 进程看到同一份配置。
from . import settings as _settings  # noqa: F401


def _env_int(name: str, default: int) -> int:
    """读取整数环境变量；为空时使用默认值，非法值直接报错而不是静默取默认。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """读取浮点环境变量。"""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _env_flag(name: str, default: bool) -> bool:
    """读取布尔环境变量；接受 1/true/yes 与 0/false/no。"""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes"}


class ConversationMiningSettings:
    """对话挖掘任务的运行时参数。

    批次大小有两个独立上限（轮次与字符数），任一先达到就切批：只按轮次切，遇到长对话会把
    单次模型请求顶到上下文上限；只按字符切，遇到大量短问答会产生过多请求。单轮问答本身超过
    字符上限时会被拆成独立批次，而不是拦腰切断——一轮问答的答案必须留在同一个批次里。
    """

    # --- 抽取模型（OpenAI 兼容 /v1/chat/completions）---
    # 未单独配置时回落到在线 Agent 使用的 OPENAI_* 配置。
    llm_base_url: str = (os.getenv("MINING_LLM_BASE_URL", "").strip() or os.getenv("OPENAI_BASE_URL", "").strip())
    llm_api_key: str = (os.getenv("MINING_LLM_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip())
    llm_model: str = (os.getenv("MINING_LLM_MODEL", "").strip() or os.getenv("OPENAI_MODEL", "").strip())
    llm_timeout: float = _env_float("MINING_LLM_TIMEOUT", 180.0)
    # 抽取是“照抄对话里已有的依据”，不需要发散，温度默认取 0。
    llm_temperature: float = _env_float("MINING_LLM_TEMPERATURE", 0.0)
    llm_max_tokens: int = _env_int("MINING_LLM_MAX_TOKENS", 2048)
    # 索引不在提示词里引用，只用于让模型按编号给出依据消息；关闭不影响结构化输出。
    # 设为 true 时要求模型返回严格 JSON（response_format=json_object），兼容性不足的服务可关闭。
    llm_use_json_mode: bool = _env_flag("MINING_LLM_USE_JSON_MODE", True)
    # 调用失败的批次最多重试几次；只重试网络与响应解析错误，结构校验失败不重试。
    llm_max_attempts: int = _env_int("MINING_LLM_MAX_ATTEMPTS", 2)

    # --- 批次切分 ---
    # 单个批次最多包含多少个完整问答轮次。
    max_turns_per_batch: int = _env_int("MINING_MAX_TURNS_PER_BATCH", 8)
    # 单个批次送进模型的对话文本字符上限；超出就切批，单轮超出则独占一批。
    max_batch_chars: int = _env_int("MINING_MAX_BATCH_CHARS", 6000)
    # 单次运行最多处理多少个会话，避免第一轮就把整库历史一次抽完。
    max_conversations_per_run: int = _env_int("MINING_MAX_CONVERSATIONS_PER_RUN", 20)
    # 单次运行最多抽取多少个批次；达到上限就停止，剩余批次留给下一次（含定时轮次）。
    max_batches_per_run: int = _env_int("MINING_MAX_BATCHES_PER_RUN", 40)
    # 单条问答候选的答案长度上限（字符）；超过一般意味着模型把整段对话抄了回来。
    max_answer_chars: int = _env_int("MINING_MAX_ANSWER_CHARS", 800)
    # 单条问答候选的问法长度上限（字符）。
    max_question_chars: int = _env_int("MINING_MAX_QUESTION_CHARS", 200)

    # --- 定时执行 ---
    # 定时周期的秒数；定时入口每次跑完等待该时长再进入下一轮。
    interval_seconds: int = _env_int("MINING_INTERVAL_SECONDS", 6 * 60 * 60)
    # 单次运行内最多重试多少个上一轮失败的批次。
    max_retry_batches: int = _env_int("MINING_MAX_RETRY_BATCHES", 3)
    # 抽取完成后是否顺势补齐 Milvus 向量；默认补齐，保证“抽取完即成为可检索知识”。
    vectorize_after_promote: bool = _env_flag("MINING_VECTORIZE_AFTER_PROMOTE", True)

    # --- 去重（语义合并）---
    # 去重分两层：问法文本归一化去掉标点后完全相同的先合并；归一化不同但语义等价的问法再用
    # BGE-M3 向量余弦相似度判定。只用文本比较无法合并“保修期是多久”与“质保多长时间”这类同义
    # 问法，同一条知识会被拆成多个块。
    #
    # 阈值按 BGE-M3 的余弦相似度取值：同义改写通常在 0.90 以上，话题相近但问题不同的句子多在
    # 0.75 到 0.88 之间，因此合并阈值取 0.88，避免把不同问题并成一条。换用其它向量模型时必须
    # 重新标定，否则合并会过松或过紧。
    question_similarity_threshold: float = _env_float("MINING_QUESTION_SIMILARITY", 0.88)
    # 答案是否视为同一答案的阈值，比问法阈值更严：答案差一点就是不同结论，不能并成一条。
    answer_similarity_threshold: float = _env_float("MINING_ANSWER_SIMILARITY", 0.95)
    # 一次去重最多为多少条不同问法计算向量；只在“归一化问法不同”时才需要调用向量服务。
    max_semantic_questions: int = _env_int("MINING_MAX_SEMANTIC_QUESTIONS", 500)
    # 向量服务不可用或未配置时是否中止入库。默认不中止：否则一个可选的外部依赖会卡死整条知识
    # 链路；此时退化为“只有归一化问法相同才合并”，并在运行结果里明确记录降级原因。
    require_embedding_for_dedupe: bool = _env_flag("MINING_REQUIRE_EMBEDDING_FOR_DEDUPE", False)

    @property
    def llm_configured(self) -> bool:
        """是否具备调用抽取模型的必要配置；缺失时任务给出可读提示而不是抛底层错误。"""
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


mining_settings = ConversationMiningSettings()
