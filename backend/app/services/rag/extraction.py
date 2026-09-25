"""历史对话的结构化问答抽取。

职责边界很明确：本模块只做“把一批对话文本交给模型，把响应解析成候选并逐条校验”，不碰数据库、
不做去重、不写正式知识。批次与暂存由 `app.services.rag.conversation_mining` 编排。

抽取的目标是**可复用的商品或服务知识**，不是聊天记录摘要。因此提示词里明确禁止把某个用户的
订单状态、物流轨迹、个人承诺或未经证实的助手回答提炼成通用政策——这类内容一旦入库，会在别的
用户提问时被当成公司政策检索出来，比缺失更有害。

模型输出按固定结构校验，任何一条不满足结构、长度、依据或脱敏要求的候选都会带上拒绝原因返回，
由调用方写入暂存表并标记状态，而不是静默丢弃。校验失败也不重试：重试同一个提示词通常得到同样
的越界输出，重试只对网络与响应解析错误有意义。

依据校验用字符 n-gram 覆盖率衡量 evidence 与原文的重合度：模型常会轻微改写或补全标点，严格
子串匹配会误杀真实依据；而覆盖率过低基本可以确认是模型凭常识补出来的内容，正是需要拦下的情形。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.config.conversation_mining import mining_settings

from .sanitization import contains_redacted_marker, redact_sensitive_text

# 候选拒绝原因：写入暂存表的 rejection_reason，便于按原因筛选与统计。
REASON_MISSING_FIELD = "缺少必填字段"
REASON_TOO_LONG = "问题或答案超出长度上限"
REASON_ANSWER_TOO_SHORT = "答案过短，不构成可复用知识"
REASON_ANSWER_IS_QUESTION = "答案本身仍是提问，未给出结论"
REASON_LOW_CONFIDENCE = "模型给出的置信度过低"
REASON_SENSITIVE_QUESTION = "问法依赖个人或交易标识，脱敏后不可复用"
REASON_SENSITIVE_ANSWER = "答案依赖个人或交易标识，脱敏后不可复用"
REASON_UNGROUNDED = "答案在对话中没有明确依据"
REASON_VAGUE = "答案含糊，未给出确定结论"
REASON_SELF_CONTRADICTORY = "答案自相矛盾"
REASON_NO_CATEGORY = "缺少可用的分类"

# 答案里出现这些词说明模型没有给出确定结论，属于含糊回答。
_VAGUE_PATTERNS = (
    "可能需要咨询",
    "建议咨询客服确认",
    "不确定",
    "不一定",
    "视情况而定",
    "看具体情况",
    "无法确定",
    "以实际为准",
    "可能会也可能不会",
    "尚不清楚",
)
# 答案里同时出现转折对比词时视为自相矛盾；单独出现不算，很多政策本身就是在讲例外。
_CONTRADICTION_PAIRS = (
    ("可以退", "不可以退"),
    ("支持退货", "不支持退货"),
    ("免费", "收费"),
    ("包邮", "不包邮"),
    ("支持换货", "不支持换货"),
    ("七天无理由", "不支持七天无理由"),
)

# 依据覆盖率阈值：evidence 与原文的字符 n-gram 重合度低于它，判定为无依据。
_GROUNDING_MIN_RATIO = 0.6
# 依据太短时 n-gram 样本太少，覆盖率不稳定，低于该长度直接用子串匹配判定。
_GROUNDING_SHORT_EVIDENCE_CHARS = 12
_NGRAM_SIZE = 4

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class ExtractionError(RuntimeError):
    """调用抽取模型失败，或响应无法解析成约定结构。"""


@dataclass(frozen=True)
class ConversationTurn:
    """一批对话中的一个完整问答轮次。

    `user_message_id` 与 `answer_message_id` 是正式知识的来源消息标识；`index` 是轮次在整批
    对话里的稳定序号，依据定位按它映射回消息。
    """

    index: int
    question: str
    answer: str
    user_message_id: str
    answer_message_id: str
    answer_role: str

    def render(self) -> str:
        """渲染成提示词里的一轮对话文本。

        角色只区分“用户”和“客服”：助手自动回答与人工客服回答对抽取都是可依据的答案来源，
        但把两者写成不同标签会让模型倾向于只信任其中一种，而实际业务里人工客服的回答往往更准。
        """
        role_label = "客服" if self.answer_role in {"assistant", "staff"} else self.answer_role
        return f"[第 {self.index} 轮]\n用户：{self.question}\n{role_label}：{self.answer}"


@dataclass(frozen=True)
class ExtractedCandidate:
    """一条通过全部校验、可以进入暂存表并参与去重的候选。"""

    question: str
    answer: str
    category: str
    evidence: str
    confidence: float | None
    source_turn_index: int


@dataclass(frozen=True)
class RejectedCandidate:
    """一条未通过校验的候选；保留原文与原因，便于人工复核抽取质量。"""

    question: str
    answer: str
    category: str
    evidence: str
    confidence: float | None
    reason: str


@dataclass
class ExtractionOutcome:
    """一次抽取的完整结果：通过校验的候选与被拒候选。"""

    candidates: list[ExtractedCandidate] = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)
    model_note: str = ""


def build_conversation_text(turns: list[ConversationTurn]) -> str:
    """把轮次渲染成送进模型的对话文本。

    轮次之间用空行分隔并带序号，使模型在给出依据时可以指明来自哪一轮，也让人工核对时能快速
    定位。文本已由调用方脱敏，这里不再做敏感信息处理。
    """
    return "\n\n".join(turn.render() for turn in turns)


SYSTEM_PROMPT = """你是电商客服知识库的抽取助手。你的任务是从历史客服对话里挑出「可复用的商品或服务知识」。

必须遵守的规则：
1. 只提取对话中有明确依据的知识；依据不足就不要输出这一条。
2. 只提取对**其他用户也成立**的商品知识或服务政策，例如尺码、材质、保修、退换货条件、发货时效、发票、支付方式。
3. 严禁把下面这些内容提炼成通用政策或通用知识：
   - 某个用户的订单状态、是否发货、金额、退款进度；
   - 某个用户的物流轨迹、快递公司、派送进度；
   - 客服针对某个用户做出的个人承诺（补偿、加急、特批、延期）；
   - 未经证实、含糊、带有“可能”“建议咨询”之类不确定表述的助手回答；
   - 纯寒暄、致谢、道歉和与商品服务无关的闲聊。
4. 问法要写成用户会真实提问的说法，不要照抄带个人信息或订单号的整句。
5. 答案必须能脱离这段对话独立理解，只写结论和条件，不要写“您”“我帮您”这类对话措辞。
6. 分类使用简短的中文业务分类，例如「退换货」「物流配送」「商品咨询」「支付发票」。

只输出 JSON，不要输出任何解释文字。输出结构：
{"items": [{"question": "用户会怎么问", "answer": "可复用的知识结论", "category": "分类", "evidence": "对话中的原文依据", "confidence": 0.0}]}

若这段对话里没有可复用的知识，输出 {"items": []}。"""


class ConversationExtractionClient:
    """调用 OpenAI 兼容对话模型抽取问答对。

    凭据与模型名来自 `app.config.conversation_mining`（默认复用 `OPENAI_*`）。本类不持有任何
    数据库会话；调用方必须先在短事务里取完数据、关闭会话之后再调用。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - openai 是后端必装依赖
            raise ExtractionError("缺少 openai 依赖，无法调用抽取模型。") from exc

        self._base_url = (base_url or mining_settings.llm_base_url).rstrip("/")
        self._model = model or mining_settings.llm_model
        self._api_key = api_key or mining_settings.llm_api_key
        if not self._base_url or not self._model or not self._api_key:
            raise ExtractionError(
                "未配置抽取模型：请设置 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL，"
                "或用 MINING_LLM_API_KEY / MINING_LLM_BASE_URL / MINING_LLM_MODEL 单独指定。"
            )
        self._client = OpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
            timeout=timeout or mining_settings.llm_timeout,
        )

    @property
    def model(self) -> str:
        """当前使用的抽取模型名，用于运行日志。"""
        return self._model

    def extract(self, turns: list[ConversationTurn]) -> ExtractionOutcome:
        """抽取并校验一批对话。

        返回通过校验的候选与被拒候选；`no_knowledge` 通过空 candidates 表达，不是错误。
        网络失败与响应无法解析为约定结构会抛 `ExtractionError`，由调用方把批次标为失败并保留
        错误信息，使下一次运行可以从该批次继续。
        """
        if not turns:
            return ExtractionOutcome()
        conversation_text = build_conversation_text(turns)
        raw = self._call_model(conversation_text)
        return validate_extraction_payload(raw, conversation_text, turns)

    def _call_model(self, conversation_text: str) -> dict:
        """发起一次对话请求并解析 JSON 响应。

        响应偶尔会带 Markdown 代码围栏或前后说明文字，这里用最外层大括号截取 JSON 片段再解析，
        避免因为一个围栏就让整批数据作废。
        """
        request: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"以下是历史客服对话：\n\n{conversation_text}"},
            ],
            "temperature": mining_settings.llm_temperature,
            "max_tokens": mining_settings.llm_max_tokens,
        }
        if mining_settings.llm_use_json_mode:
            request["response_format"] = {"type": "json_object"}
        try:
            response = self._client.chat.completions.create(**request)
        except Exception as exc:
            raise ExtractionError(f"调用抽取模型失败：{type(exc).__name__}: {exc}") from exc

        if not response.choices:
            raise ExtractionError("抽取模型返回了空的选择列表。")
        content = response.choices[0].message.content or ""
        match = _JSON_BLOCK.search(content)
        if not match:
            raise ExtractionError("抽取模型返回的内容中没有 JSON 对象。")
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"抽取模型返回的 JSON 无法解析：{exc}") from exc
        if not isinstance(payload, dict):
            raise ExtractionError("抽取模型返回的 JSON 顶层不是对象。")
        return payload


def _as_text(value: object) -> str:
    """把模型返回的字段规整成去空白字符串；非字符串类型直接视为无效内容。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _as_confidence(value: object) -> float | None:
    """解析置信度；越界或非数值时返回 None，由后续规则按“未给出置信度”处理。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if 0.0 <= number <= 1.0 else None
    if isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
        return number if 0.0 <= number <= 1.0 else None
    return None


def _resolve_source_turn(evidence: str, turns: list[ConversationTurn]) -> int:
    """按依据与各轮文本的重合度定位来源轮次，返回轮次序号。

    模型给出的依据通常近似某一段原文，用字符重合度取最匹配的一轮即可；完全没有重合时返回
    第一轮的序号，此时候选已被依据校验拒绝，来源标识只用于把被拒候选也记录成可追溯的行。
    """
    if not evidence:
        return turns[0].index
    grams = _ngrams(evidence)
    if not grams:
        return turns[0].index
    best_index = turns[0].index
    best_hits = -1
    for turn in turns:
        turn_grams = _ngrams(turn.render())
        hits = len(grams & turn_grams)
        if hits > best_hits:
            best_hits = hits
            best_index = turn.index
    return best_index


def _ngrams(text: str, size: int = _NGRAM_SIZE) -> set[str]:
    """把文本切成字符 n-gram 集合，用于衡量依据与原文的重合度。

    先去掉空白，使换行与空格差异不影响比较；文本短于 n 时退化为整串一个元素。
    """
    compact = re.sub(r"\s+", "", text)
    if len(compact) <= size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def evidence_is_grounded(evidence: str, conversation_text: str) -> bool:
    """判断依据是否确实来自对话原文。

    短依据用子串匹配，避免 n-gram 样本不足导致比例失真；长依据用 n-gram 覆盖率，容忍模型
    补标点、换行和轻微改写，同时拦下凭常识编出来的“依据”。
    """
    compact_evidence = re.sub(r"\s+", "", evidence)
    compact_conversation = re.sub(r"\s+", "", conversation_text)
    if not compact_evidence:
        return False
    if len(compact_evidence) <= _GROUNDING_SHORT_EVIDENCE_CHARS:
        return compact_evidence in compact_conversation
    grams = _ngrams(evidence)
    if not grams:
        return False
    covered = len(grams & _ngrams(conversation_text)) / len(grams)
    return covered >= _GROUNDING_MIN_RATIO


def _answer_is_vague(answer: str) -> bool:
    """判断答案是否为没有结论的含糊表述。"""
    return any(pattern in answer for pattern in _VAGUE_PATTERNS)


def _answer_is_self_contradictory(answer: str) -> bool:
    """判断答案是否同时给出互相排斥的结论。"""
    return any(first in answer and second in answer for first, second in _CONTRADICTION_PAIRS)


def validate_extraction_payload(
    payload: dict,
    conversation_text: str,
    turns: list[ConversationTurn],
) -> ExtractionOutcome:
    """按固定结构逐条校验模型输出。

    校验通过的候选已完成脱敏；被拒候选同样保留脱敏后的文本，原因写入 `reason`。这里假定
    `conversation_text` 已经是脱敏文本——候选的问法与答案就是对它的提炼，因此只要对话文本干净，
    输出里就不可能出现被抹掉的敏感值。
    """
    if not isinstance(payload, dict):
        raise ExtractionError("抽取模型返回的结构不是对象。")
    items = payload.get("items")
    if items is None:
        raise ExtractionError("抽取模型返回的结构缺少 items 字段。")
    if not isinstance(items, list):
        raise ExtractionError("抽取模型返回的 items 不是数组。")

    outcome = ExtractionOutcome()
    for item in items:
        if not isinstance(item, dict):
            outcome.rejected.append(
                RejectedCandidate(
                    question="", answer="", category="", evidence="", confidence=None,
                    reason=REASON_MISSING_FIELD,
                )
            )
            continue
        candidate, reason = _validate_item(item, conversation_text, turns)
        if candidate is not None:
            outcome.candidates.append(candidate)
        elif reason is not None:
            outcome.rejected.append(reason)
    return outcome


def _validate_item(
    item: dict, conversation_text: str, turns: list[ConversationTurn]
) -> tuple[ExtractedCandidate | None, RejectedCandidate | None]:
    """校验单条候选，返回“通过”或“被拒”，两者恰有其一非空。"""
    # 模型输出也要过一遍脱敏：提示词已经要求不要照抄标识，但它仍可能把订单号写进答案。
    question = redact_sensitive_text(_as_text(item.get("question")))
    answer = redact_sensitive_text(_as_text(item.get("answer")))
    category = _as_text(item.get("category"))
    evidence = _as_text(item.get("evidence"))
    confidence = _as_confidence(item.get("confidence"))

    def reject(reason: str) -> tuple[None, RejectedCandidate]:
        return None, RejectedCandidate(
            question=question, answer=answer, category=category,
            evidence=evidence, confidence=confidence, reason=reason,
        )

    if not question or not answer or not evidence:
        return reject(REASON_MISSING_FIELD)
    if len(question) > mining_settings.max_question_chars or len(answer) > mining_settings.max_answer_chars:
        return reject(REASON_TOO_LONG)
    if len(answer) < 8:
        return reject(REASON_ANSWER_TOO_SHORT)
    if answer.rstrip().endswith(("？", "?")):
        return reject(REASON_ANSWER_IS_QUESTION)
    if confidence is not None and confidence < 0.5:
        return reject(REASON_LOW_CONFIDENCE)
    # 脱敏后仍带占位符，说明这条知识只在某个用户的具体情境下成立，不能作为通用知识。
    if contains_redacted_marker(question):
        return reject(REASON_SENSITIVE_QUESTION)
    if contains_redacted_marker(answer):
        return reject(REASON_SENSITIVE_ANSWER)
    if _answer_is_vague(answer):
        return reject(REASON_VAGUE)
    if _answer_is_self_contradictory(answer):
        return reject(REASON_SELF_CONTRADICTORY)
    if not evidence_is_grounded(evidence, conversation_text):
        return reject(REASON_UNGROUNDED)
    if not category:
        return reject(REASON_NO_CATEGORY)

    return (
        ExtractedCandidate(
            question=question,
            answer=answer,
            category=category,
            evidence=evidence,
            confidence=confidence,
            source_turn_index=_resolve_source_turn(evidence, turns),
        ),
        None,
    )
