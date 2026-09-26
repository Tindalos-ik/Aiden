"""对话挖掘编排：消息 -> 分批抽取 -> 暂存 -> 整体去重 -> knowledge_chunks -> Milvus。

这是本阶段的调用链中枢，顺序是刻意固定的：

1. **读消息并切批。** 只读 `complete` 状态的消息，按“用户提问 + 紧邻的可信答案”组成完整问答轮次，
   再把轮次按轮数与字符数上限装箱成批次。切批只发生在轮次边界上，一轮问答不会被切到两个批次里，
   否则答案会与问题分离，抽取出来的知识必然残缺。
2. **逐批调模型抽取。** 调用前对整段对话脱敏；模型输出按固定结构逐条校验，通过校验的候选与被拒
   候选都写入暂存表。这一步**只写暂存**，不碰 `knowledge_chunks`。
3. **等全部待处理批次抽取完再整体去重。** 未完成批次可能属于旧轮次或尚无 `run_id`；只要仍有
   `pending`、`extracting` 或 `failed`，就推迟去重。下一轮同时读取旧轮次和新轮次的暂存候选。
4. **去重后按答案分组入库。** 相同答案的不同真实问法合并成一条知识（questions 保存全部问法）；
   答案冲突的候选留在暂存表标为待处理；与已有知识答案一致的候选视为已入库，避免重复生成正式知识
   与重复向量。
5. **复用第一阶段的向量化流程。** 入库只调用 `knowledge_repo.upsert_source_chunks`，向量补齐只调用
   `indexing.vectorize_pending`；本模块不另写向量化或双写逻辑。

**Session 边界。** 每步之间的数据库访问都在 `app.persistence.mysql.conversation_mining` 的短事务里
完成，调用模型期间没有任何打开的 Session；`upsert_source_chunks` 内部也是独立短事务。

**幂等性。** 批次签名由会话与窗口内消息序列决定，候选键由批次签名与序号决定，知识块来源标识由
规范化问法集合决定，chunk_key 又由来源、序号与内容哈希决定。因此重复运行只会命中已有行：已抽取
的窗口不会再次调模型，已入库的知识不会产生新块，Milvus 侧是 upsert 覆盖同一条向量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Callable, Iterable

from app.config.conversation_mining import mining_settings
from app.persistence.mysql import knowledge as knowledge_repo
from app.persistence.mysql import conversation_mining as staging_repo
from app.persistence.mysql.conversation_mining import (
    MiningBatchRow,
    MiningMessageRow,
    answer_text_equal,
)

from .embedding_text import build_embedding_text, embedding_fingerprint
from .extraction import (
    ConversationExtractionClient,
    ConversationTurn,
    ExtractedCandidate,
    ExtractionError,
    RejectedCandidate,
)
from .knowledge import content_hash
from .sanitization import redact_sensitive_text

# 正式知识的来源类型与内容类型。
# source_type 复用 0004 迁移中已允许的 'conversation'；content_type 标为可读的“对话挖掘问答”，
# 便于在 knowledge_chunks 与 Milvus 里区分“文档切分出来的块”和“从历史对话挖出来的问答”。
SOURCE_TYPE_CONVERSATION = "conversation"
CONTENT_TYPE_MINED_QA = "对话挖掘问答"
# 来源标识前缀。来源标识按“规范化问法集合”确定，因此同一组真实问法重复入库时只覆盖同一行。
SOURCE_PREFIX = "conversation-qa"

# 去重阶段的拒绝原因。冲突只有一个原因文案：批次内答案不一致与“与已有知识冲突”对人工复核是同一类
# 待办（确认哪个版本才是对的），合成一条反而更容易按原因筛选。
REASON_DUPLICATE_OF_EXISTING = "与已入库知识重复，未重复生成"
REASON_ANSWER_CONFLICT = "同一问法存在相互冲突的答案，留待人工确认后入库"

# 无法抽取的窗口的原因。
REASON_OVERSIZED_TURN = "单轮问答超过单批字符上限，需人工拆分后重跑"
# 批次没有可入库的新知识（包括没有有效候选）时，仍需记录结论并推进续跑锚点。
REASON_BATCH_NO_NEW_KNOWLEDGE = "本批次无可入库的新知识，候选结论已记录"

# 规范化问法时去掉的标点：全角与半角都要覆盖，否则“怎么退货？”与“怎么退货”会被当成两种问法。
_PUNCTUATION = re.compile(r"[\s\u3000!-/:-@\[-`{-~！-＠［-｀｛-～、。，；：？！“”‘’（）《》【】—…·]+")


@dataclass
class ExtractionRunResult:
    """一次抽取阶段的统计，用于命令行输出说明实际处理了多少数据。"""

    run_id: str
    conversations_examined: int
    batches_created: int
    batches_extracted: int
    candidates_staged: int
    candidates_rejected: int
    batches_failed: int
    batches_skipped: int
    pending_batches_left: int


@dataclass
class PromotionRunResult:
    """一次整体去重与入库的统计。"""

    run_id: str
    staged_candidates: int
    distinct_questions: int
    promoted_knowledge: int
    merged_candidates: int
    promoted_candidates: int
    duplicate_candidates: int
    conflict_candidates: int
    batches_promoted: int
    vectors_written: int
    notes: list[str] = field(default_factory=list)


@dataclass
class PipelineResult:
    """一次任务的统计；`skipped_dedupe` 为真表示待处理池尚不完整，去重被推迟。"""

    extraction: ExtractionRunResult
    promotion: PromotionRunResult | None = None
    skipped_dedupe: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AnswerGroup:
    """整体去重的分组结果：同一答案（指纹一致）与同一问法的候选集合。"""

    key: str
    question: str
    answer: str
    category: str
    candidates: list[staging_repo.StagedCandidate]


@dataclass(frozen=True)
class _KnowledgeEntry:
    """待入库知识中的一个答案分组：真实问法相同、答案指纹一致的一批候选。"""

    group: _AnswerGroup
    candidates: list[staging_repo.StagedCandidate]


@dataclass
class _KnowledgeItem:
    """一条待入库知识：一个规范化问法下全部语义等价的答案分组。

    正常情况下只有一个 entry；只有当同一问法出现了措辞不同但语义等价的答案（例如“7 个工作日”
    与“七个工作日”）时才会有多个 entry，此时它们合并成一条知识，questions 收集全部真实问法。
    """

    question_key: str
    category: str
    entries: list[_KnowledgeEntry]
    answer: str = ""

    def __post_init__(self) -> None:
        # 代表答案取最长的一条：语义等价时，更长的表述信息更完整，也更适合作为检索返回内容。
        if not self.answer:
            self.answer = max((entry.group.answer for entry in self.entries), key=len)

    @property
    def questions(self) -> list[str]:
        """本条知识的全部真实问法原文；保留原文而不是规范化结果，正式知识要用用户真实说法。"""
        return [
            question
            for question in dict.fromkeys(
                entry.group.question.strip() for entry in self.entries if entry.group.question.strip()
            )
        ]


@dataclass(frozen=True)
class _ExistingKnowledge:
    """已有正式知识中的一条问答，供去重时比较问法与答案。"""

    question: str
    question_key: str
    answer: str
    chunk_id: str


@dataclass(frozen=True)
class _DedupePlan:
    """整体去重的输入索引，由 `build_dedupe_plan` 从暂存候选与已有知识构建。"""

    # 规范化问法 -> 该问法下的答案分组（组数大于 1 即同一问法出现多个答案）。
    by_question: dict[str, list[_AnswerGroup]]
    # 已有正式知识的问答条目；问法与答案的等价判断都在它上面做。
    existing: list[_ExistingKnowledge]
    distinct_question_forms: int


def normalize_question(question: str) -> str:
    """规范化问法：去掉空白与标点后作为“问法是否相同”的判定依据。

    只用于分组与展示去重，不改变入库的 questions 文本——正式知识必须保留对话中的真实问法原文，
    规范化后的形式仅在内部比较时使用。
    """
    return _PUNCTUATION.sub("", question).strip().lower()


def _question_group_key(question: str) -> str:
    """问法分组的稳定键；同时用作知识来源标识的一部分。"""
    return sha256(normalize_question(question).encode("utf-8")).hexdigest()


def _source_id_for_questions(questions: Iterable[str]) -> str:
    """按规范化问法集合生成知识来源标识。

    用问法集合而不是会话或批次来标识来源，是为了让“同一组真实问法”在每次运行中都映射到同一个
    来源：`upsert_source_chunks` 的作废判定是“同一来源内本次未生成的块”，来源稳定，重复运行才
    不会把已经向量化的块置为 superseded。问法集合排序后参与哈希，因此问法顺序变化不影响标识。
    """
    normalized = sorted({normalize_question(question) for question in questions if normalize_question(question)})
    digest = sha256("\n".join(normalized).encode("utf-8")).hexdigest()
    return f"{SOURCE_PREFIX}:{digest[:40]}"


def build_turns(messages: list[MiningMessageRow]) -> list[tuple[int, MiningMessageRow, MiningMessageRow]]:
    """把消息序列组织成完整问答轮次。

    规则：一条用户消息（或**连续**多条用户消息合并成一个提问）加上紧邻其后的一条可信答案消息
    （`assistant` 或 `staff`，且状态为 `complete`）构成一轮。连续用户消息合并是必要的，否则
    “我要退货”和“尺码不对”会被当成两个独立问题，答案却只有一个。

    没有等到答案的末尾用户消息被丢弃（不是完整轮次），等下一轮运行有新答案时再处理；`system`
    消息不参与，它不是知识依据。

    返回 `(序号, 用户消息, 答案消息)`，序号从 0 开始且只依赖消息顺序，因此重跑同一批消息得到
    完全相同的编号，依据定位与候选幂等键都基于它。
    """
    turns: list[tuple[int, MiningMessageRow, MiningMessageRow]] = []
    pending_questions: list[MiningMessageRow] = []
    for message in messages:
        if message.sender_role == "user":
            pending_questions.append(message)
            continue
        if message.sender_role not in staging_repo.ANSWER_ROLES:
            # system 消息只提供系统提示，不是可信答案来源。
            continue
        if not pending_questions:
            # 开头的助手消息（例如欢迎语）没有对应提问，不构成轮次。
            continue
        if not message.content.strip():
            # 空内容的 complete 消息不能当答案，保留待答提问等下一个答案。
            continue
        turns.append((len(turns), pending_questions[0], message))
        # 合并后的提问只归属这一轮；多条用户消息已并入同一轮答案。
        pending_questions = []
    return turns


def _turns_to_conversation_turns(
    turns: list[tuple[int, MiningMessageRow, MiningMessageRow]]
) -> list[ConversationTurn]:
    """把消息级轮次转成抽取模块使用的轮次对象，并在这里完成脱敏。

    脱敏放在这里而不是模型响应之后：送进模型的内容本身就不该带手机号、地址、订单号，模型即使
    照抄也抄不到敏感值。
    """
    return [
        ConversationTurn(
            index=index,
            question=redact_sensitive_text(user_message.content.strip()),
            answer=redact_sensitive_text(answer_message.content.strip()),
            user_message_id=user_message.message_id,
            answer_message_id=answer_message.message_id,
            answer_role=answer_message.sender_role,
        )
        for index, user_message, answer_message in turns
    ]


def _pack_conversation_turns(
    conversation_id: str,
    turns: list[ConversationTurn],
    message_by_id: dict[str, MiningMessageRow],
) -> tuple[int, int]:
    """把一个会话的问答轮次按上限装箱并落库，返回 `(新建批次数, 未新建的窗口数)`。

    两个上限同时生效：`MINING_MAX_TURNS_PER_BATCH` 控制单批轮次数量，`MINING_MAX_BATCH_CHARS`
    控制单批送进模型的字符数。装箱按轮次边界进行，因此一轮问答永远落在同一个批次里。

    单轮自身就超过字符上限时，它独占一个批次：与别的轮次合并只会让整批超限，切开又违反“一轮问答
    不切分”的约束，所以让它独立成批，抽取阶段再标记为 skipped 并记录原因。

    返回值里的第二个数是“签名已存在的窗口数”，用于判断本次是否真的还有新工作；已存在的窗口说明
    之前已经处理过，不计入新建。
    """
    created = 0
    existing = 0
    current: list[ConversationTurn] = []
    current_chars = 0

    def flush() -> None:
        """把当前累计的轮次落成一个待抽取批次。"""
        nonlocal current, current_chars, created, existing
        if not current:
            return
        message_ids: list[str] = []
        for turn in current:
            message_ids.extend((turn.user_message_id, turn.answer_message_id))
        first = message_by_id[message_ids[0]]
        last = message_by_id[message_ids[-1]]
        signature = staging_repo.batch_signature(
            conversation_id, message_ids, [message_by_id[mid].sender_role for mid in message_ids]
        )
        batch_id = staging_repo.create_batch_if_absent(
            conversation_id=conversation_id,
            signature=signature,
            message_ids=message_ids,
            first_message_at=first.created_at,
            last_message_at=last.created_at,
            turn_count=len(current),
        )
        if batch_id is None:
            existing += 1
        else:
            created += 1
        current = []
        current_chars = 0

    for turn in turns:
        turn_chars = len(turn.question) + len(turn.answer)
        if current and (
            len(current) >= mining_settings.max_turns_per_batch
            or current_chars + turn_chars > mining_settings.max_batch_chars
        ):
            flush()
        current.append(turn)
        current_chars += turn_chars
        if turn_chars > mining_settings.max_batch_chars:
            # 单轮就超限：独立成批，抽取阶段会把它标记为 skipped 并推进锚点。
            flush()
    flush()
    return created, existing


def create_batches_for_run(*, run_limit: int) -> tuple[int, int]:
    """为增量消息创建待抽取批次，返回 `(检查的会话数, 新建批次数)`。

    读取是增量的：每个会话从“已处理批次的最后消息时间”之后继续读，已处理过的消息不会被再次读取，
    因此重复运行不会重复抽取同一批消息。会话按最早活动时间升序处理，积压数据按时间顺序补齐。
    新建批次数达到 `run_limit` 时停止，剩余消息留给下一次运行。
    """
    conversation_ids = staging_repo.list_conversations_with_trusted_messages(
        mining_settings.max_conversations_per_run
    )
    created = 0
    for conversation_id in conversation_ids:
        if created >= run_limit:
            break
        anchor = staging_repo.last_terminal_message_at(conversation_id)
        messages = staging_repo.list_trusted_messages(conversation_id, after=anchor)
        if not messages:
            continue
        message_by_id = {message.message_id: message for message in messages}
        turns = _turns_to_conversation_turns(build_turns(messages))
        if not turns:
            continue
        conversation_created, _ = _pack_conversation_turns(conversation_id, turns, message_by_id)
        created += conversation_created
    return len(conversation_ids), created


def _oversized_turns(turns: list[ConversationTurn]) -> bool:
    """判断这批对话是否因单轮过大而无法抽取。"""
    return len(turns) == 1 and (
        len(turns[0].question) + len(turns[0].answer) > mining_settings.max_batch_chars
    )


def run_extraction(
    *,
    run_id: str,
    client: ConversationExtractionClient,
    max_batches: int | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ExtractionRunResult:
    """执行抽取阶段：读消息、切批、逐批调模型、写暂存。

    只做抽取与暂存。待处理批次全部抽取完成后由 `run_promotion` 做整体去重与入库；
    旧轮次已抽取的批次会保留在暂存池，直至全部批次可一起去重。
    """
    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    batch_budget = max_batches if max_batches is not None else mining_settings.max_batches_per_run
    conversations, batches_created = create_batches_for_run(run_limit=batch_budget)
    report(f"检查会话 {conversations} 个，新建抽取批次 {batches_created} 个")

    extracted = 0
    failed = 0
    skipped = 0
    staged = 0
    rejected = 0
    processed = 0

    while processed < batch_budget:
        claimed = staging_repo.claim_pending_batches(run_id, limit=batch_budget - processed)
        if not claimed:
            break
        for batch in claimed:
            processed += 1
            messages = _read_batch_messages(batch)
            turns = _turns_to_conversation_turns(build_turns(messages))
            if not turns:
                # 窗口内没有完整轮次（例如答案消息被清空）：没有任何可抽取内容，
                # 标记跳过并推进锚点，避免每次运行都重复读同一段历史。
                staging_repo.mark_batch_skipped(batch.id, "窗口内没有完整问答轮次")
                skipped += 1
                continue
            if _oversized_turns(turns):
                staging_repo.mark_batch_skipped(batch.id, REASON_OVERSIZED_TURN)
                skipped += 1
                report(f"批次 {batch.id[:8]} 单轮超长，已跳过并记录原因")
                continue
            try:
                outcome = client.extract(turns)
            except ExtractionError as exc:
                # 只标记失败并保留原因，不改动候选；下一次运行会重新认领这个批次继续抽取。
                staging_repo.mark_batch_failed(batch.id, str(exc))
                failed += 1
                report(f"批次 {batch.id[:8]} 抽取失败：{exc}")
                continue

            drafts = _candidate_drafts(batch, turns, outcome.candidates, outcome.rejected)
            staging_repo.upsert_candidates(drafts)
            staging_repo.mark_batch_extracted(batch.id, candidate_count=len(drafts))
            extracted += 1
            staged += len(outcome.candidates)
            rejected += len(outcome.rejected)

    pending_left = staging_repo.count_batches_by_status().get(staging_repo.BATCH_STATUS_PENDING, 0)
    return ExtractionRunResult(
        run_id=run_id,
        conversations_examined=conversations,
        batches_created=batches_created,
        batches_extracted=extracted,
        candidates_staged=staged,
        candidates_rejected=rejected,
        batches_failed=failed,
        batches_skipped=skipped,
        pending_batches_left=pending_left,
    )


def _read_batch_messages(batch: MiningBatchRow) -> list[MiningMessageRow]:
    """按批次记录的消息顺序读取消息内容。

    不按时间重新查询而是按 `message_ids` 取回，是为了让抽取看到的顺序与建立批次签名时的顺序完全
    一致；重跑同一批次时上下文不变，模型看到的输入也完全一样。
    """
    messages = staging_repo.list_trusted_messages(
        batch.conversation_id, after=None, limit=staging_repo.MESSAGE_READ_LIMIT
    )
    by_id = {message.message_id: message for message in messages}
    return [by_id[message_id] for message_id in batch.message_ids if message_id in by_id]


def _resolve_turn_messages(
    turns: list[ConversationTurn], turn_index: int
) -> tuple[str, str]:
    """把轮次序号映射回来源消息标识；序号不在范围内时退回第一轮。"""
    for turn in turns:
        if turn.index == turn_index:
            return turn.user_message_id, turn.answer_message_id
    return turns[0].user_message_id, turns[0].answer_message_id


def _candidate_drafts(
    batch: MiningBatchRow,
    turns: list[ConversationTurn],
    candidates: list[ExtractedCandidate],
    rejected: list[RejectedCandidate],
) -> list[staging_repo.CandidateDraft]:
    """把抽取结果转成暂存表行。

    候选的 `candidate_key` 由批次签名与序号决定，因此重复抽取同一批消息只会覆盖同一行；被拒候选
    也写入暂存表并带拒绝原因，保留在库里供人工复核，而不是静默丢弃。
    """
    drafts: list[staging_repo.CandidateDraft] = []
    for index, candidate in enumerate(candidates):
        user_message_id, answer_message_id = _resolve_turn_messages(turns, candidate.source_turn_index)
        drafts.append(
            staging_repo.CandidateDraft(
                batch_id=batch.id,
                batch_signature=batch.batch_signature,
                conversation_id=batch.conversation_id,
                candidate_key=staging_repo.candidate_key(batch.batch_signature, index),
                question=candidate.question,
                answer=candidate.answer,
                category=candidate.category,
                source_user_message_id=user_message_id,
                source_answer_message_id=answer_message_id,
                evidence=candidate.evidence,
                confidence=candidate.confidence,
                status=staging_repo.CANDIDATE_STATUS_STAGED,
                rejection_reason=None,
                answer_fingerprint=staging_repo.answer_fingerprint(candidate.answer),
            )
        )
    offset = len(candidates)
    for index, item in enumerate(rejected):
        user_message_id, answer_message_id = _resolve_turn_messages(turns, 0)
        drafts.append(
            staging_repo.CandidateDraft(
                batch_id=batch.id,
                batch_signature=batch.batch_signature,
                conversation_id=batch.conversation_id,
                candidate_key=staging_repo.candidate_key(batch.batch_signature, offset + index),
                question=item.question,
                answer=item.answer,
                category=item.category,
                source_user_message_id=user_message_id,
                source_answer_message_id=answer_message_id,
                evidence=item.evidence,
                confidence=item.confidence,
                status=staging_repo.CANDIDATE_STATUS_REJECTED,
                rejection_reason=item.reason,
                answer_fingerprint=staging_repo.answer_fingerprint(item.answer) if item.answer else "",
            )
        )
    return drafts


def group_candidates(
    candidates: list[staging_repo.StagedCandidate],
) -> list[_AnswerGroup]:
    """按“同一问法 + 同一答案”分组，供整体去重。

    分组键同时包含规范化问法与答案指纹：同一问法下答案不同会自然落到两个组，正是“答案冲突”
    需要识别出来的情形。分组在**本轮全部候选**上进行，所以跨批次的重复问法会被合并。
    """
    groups: dict[str, list[staging_repo.StagedCandidate]] = {}
    for candidate in candidates:
        key = f"{_question_group_key(candidate.question)}\n{candidate.answer_fingerprint}"
        groups.setdefault(key, []).append(candidate)

    result: list[_AnswerGroup] = []
    for key, items in groups.items():
        first = items[0]
        result.append(
            _AnswerGroup(
                key=key,
                question=first.question,
                answer=first.answer,
                category=first.category,
                candidates=items,
            )
        )
    return result


def _pick_category(groups: list[_AnswerGroup]) -> str:
    """从同一问法下的多个分组中选一个分类：取出现次数最多的分类，平票时取字典序最小者。

    同一问法不同分类通常意味着模型分类不稳定，取多数并保持确定性，避免每次运行得到不同分类而
    产生不同 chunk_key。
    """
    counts: dict[str, int] = {}
    for group in groups:
        category = group.category.strip()
        if category:
            counts[category] = counts.get(category, 0) + len(group.candidates)
    if not counts:
        return "未分类"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


class _SemanticSimilarity:
    """去重用的语义比较：惰性计算向量，向量服务不可用时退化为纯文本比较。

    复用第一阶段的 `EmbeddingClient`（同一个 BGE-M3 服务与维度校验），本模块不新建向量客户端。
    向量只在第一次需要比较“归一化问法不同”的文本时才批量计算一次，纯文本能判定的情况不付这个
    代价；计算失败时把 `note` 置为降级说明，并按 `require_embeddings` 决定是中止还是继续。

    需要说明它**不用于入库**：正式知识的向量仍由第一阶段的 `vectorize_pending` 生成，这里只是
    借同一个模型判断两条候选是否在说同一件事。
    """

    def __init__(
        self,
        *,
        client: object | None = None,
        question_threshold: float | None = None,
        answer_threshold: float | None = None,
        require_embeddings: bool | None = None,
        max_texts: int | None = None,
    ) -> None:
        self._client = client
        self._client_error: Exception | None = None
        self._vectors: dict[str, list[float]] = {}
        self._attempted = False
        self.note: str | None = None
        self.question_threshold = (
            mining_settings.question_similarity_threshold if question_threshold is None else question_threshold
        )
        self.answer_threshold = (
            mining_settings.answer_similarity_threshold if answer_threshold is None else answer_threshold
        )
        self.require_embeddings = (
            mining_settings.require_embedding_for_dedupe if require_embeddings is None else require_embeddings
        )
        self.max_texts = mining_settings.max_semantic_questions if max_texts is None else max_texts

    def prime(self, texts: list[str]) -> None:
        """预先批量计算将要比较的文本向量，避免逐对比较时重复调用向量服务。"""
        unique = [text for text in dict.fromkeys(texts) if text]
        if not unique or self._attempted:
            return
        self._attempted = True
        try:
            client = self._get_client()
            vectors = client.embed_documents(unique[: self.max_texts])
        except Exception as exc:  # 服务未启动、维度不符、超时等都按“语义比较不可用”处理
            self._client_error = exc
            if self.require_embeddings:
                raise
            self.note = (
                "向量服务不可用，本次去重退化为纯文本比较（只有归一化问法完全相同的候选才会合并）："
                f"{type(exc).__name__}: {exc}"
            )
            return
        for text, vector in zip(unique, vectors):
            self._vectors[text] = vector

    def _get_client(self):
        """惰性创建 BGE-M3 客户端；复用第一阶段的 EmbeddingClient 实现。"""
        if self._client is None:
            from .embedding import EmbeddingClient

            self._client = EmbeddingClient()
        return self._client

    def equivalent(self, left: str, right: str, *, threshold: float) -> bool:
        """判断两段文本是否语义等价。"""
        if left == right:
            return True
        vectors = self._vectors
        if left not in vectors or right not in vectors:
            return False
        return _cosine(vectors[left], vectors[right]) >= threshold

    @property
    def available(self) -> bool:
        """是否具备语义比较能力（至少算成功过一对向量）。"""
        return bool(self._vectors)


def _new_similarity() -> _SemanticSimilarity:
    """按当前配置创建语义比较器。"""
    return _SemanticSimilarity()


def _cosine(left: list[float], right: list[float]) -> float:
    """计算余弦相似度；任一向量为零向量时返回 0，避免除零。"""
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _answers_equivalent(left: str, right: str, similarity: _SemanticSimilarity) -> bool:
    """判断两条答案是否同一答案。

    先用规范化文本比较（快且确定）；文本不同时再用答案阈值做语义比较，覆盖“7 个工作日”与
    “七个工作日”、“12 个月”与“一年”这类同义表述。语义比较不可用时只认文本完全相同。
    """
    if answer_text_equal(left, right):
        return True
    if not similarity.available:
        return False
    return similarity.equivalent(
        _normalized_answer(left), _normalized_answer(right), threshold=similarity.answer_threshold
    )


def _answer_groups_equivalent(
    groups: list[_AnswerGroup], similarity: _SemanticSimilarity
) -> bool:
    """判断同一问法下的多个答案分组是否互为同一答案（语义等价即视为同一答案）。"""
    reference = groups[0].answer
    return all(_answers_equivalent(reference, group.answer, similarity) for group in groups[1:])


def _normalized_answer(answer: str) -> str:
    """答案的规范化形式，用于语义比较；去掉空白避免排版差异影响向量。"""
    return "".join(answer.split())


def build_dedupe_plan(
    staged: list[staging_repo.StagedCandidate],
    existing: list[staging_repo.ExistingChunk],
) -> _DedupePlan:
    """准备整体去重所需的分组数据。

    `by_question` 按**本轮全部候选**分组（跨对话、跨批次），所以同一问法在本轮出现多个答案时会被
    识别出来；`existing` 把已有正式知识拆成“问法 + 答案”的条目，既有知识里一条问答知识可能带多个
    真实问法，拆开后才能对每个问法分别判断是否已经入库。
    """
    by_question: dict[str, list[_AnswerGroup]] = {}
    for group in group_candidates(staged):
        by_question.setdefault(_question_group_key(group.question), []).append(group)

    existing_entries: list[_ExistingKnowledge] = []
    for chunk in existing:
        for question in chunk.questions:
            existing_entries.append(
                _ExistingKnowledge(
                    question=question,
                    question_key=_question_group_key(question),
                    answer=chunk.answer,
                    chunk_id=chunk.id,
                )
            )
    return _DedupePlan(
        by_question=by_question,
        existing=existing_entries,
        distinct_question_forms=len(by_question),
    )


def _semantic_texts(plan: _DedupePlan) -> list[str]:
    """列出需要计算向量的文本：已有知识与本轮候选中出现过的全部问法原文和答案。

    语义比较要判定的恰恰是“文本不同但意思相同”的情形，因此不能只挑文本差异明显的子集：同一问法
    下措辞不同的答案要比较，问法不同但同义的候选也要比较。这里把所有文本去重后一次算完向量，避免
    逐对比较时反复调用向量服务。
    """
    texts: list[str] = []
    for item in plan.existing:
        texts.append(normalize_question(item.question))
        texts.append(_normalized_answer(item.answer))
    for groups in plan.by_question.values():
        for group in groups:
            texts.append(normalize_question(group.question))
            texts.append(_normalized_answer(group.answer))
    return texts


def dedupe_candidates(
    plan: _DedupePlan,
    *,
    similarity: _SemanticSimilarity | None = None,
) -> tuple[list[_KnowledgeItem], list[tuple[str, str]], str | None]:
    """整体去重的纯函数部分：决定哪些候选入库、哪些留待处理。

    返回 `(待入库知识, [(候选主键, 拒绝原因)], 降级提示)`。这里不碰数据库，判定规则可以单独核对。

    判定顺序：

    1. 同一问法在本轮出现多个**不互为同义**的答案 -> 冲突，全部留待人工确认；
    2. 该问法与已有正式知识相同或同义，且候选答案与已有答案等价 -> 已入库，不重复生成；候选答案
       与已有答案不等价 -> 冲突，留待人工确认，不自动改写已有知识；
    3. 其余候选先按“问法 + 答案”分组，再把语义等价的问法合并成一条知识，questions 保留全部真实
       问法。

    同一条知识里出现多个分组只有一种情形：同一问法下答案措辞不同但语义等价（例如“7 个工作日”
    与“七个工作日”）。这时取最长的表述作为答案，全部真实问法一起写进 questions。

    合并范围是**本轮全部候选**（跨对话、跨批次），不是单个模型批次内部，这正是整体去重的意义。
    """
    semantic = similarity or _SemanticSimilarity()
    semantic.prime(_semantic_texts(plan))
    items: list[_KnowledgeItem] = []
    rejected: list[tuple[str, str]] = []

    for question_key in sorted(plan.by_question):
        groups = plan.by_question[question_key]

        def reject_all(reason: str) -> None:
            for group in groups:
                for candidate in group.candidates:
                    rejected.append((candidate.id, reason))

        # 规则 1：本轮内部同一问法出现不同答案。措辞不同但语义等价的答案不算冲突。
        if len(groups) > 1 and not _answer_groups_equivalent(groups, semantic):
            reject_all(REASON_ANSWER_CONFLICT)
            continue

        # 规则 2：已有正式知识里存在同一或同义问法。
        related = _related_existing(plan, groups, semantic)
        if related:
            candidate_answers = [group.answer for group in groups]
            if all(
                any(_answers_equivalent(answer, item.answer, semantic) for item in related)
                for answer in candidate_answers
            ):
                reject_all(REASON_DUPLICATE_OF_EXISTING)
            else:
                # 同一问法已有不同结论的知识：冲突，留待人工确认，不自动改写已有知识。
                reject_all(REASON_ANSWER_CONFLICT)
            continue

        items.append(
            _KnowledgeItem(
                question_key=question_key,
                category=_pick_category(groups),
                entries=[_KnowledgeEntry(group=group, candidates=group.candidates) for group in groups],
            )
        )

    merged = _merge_by_semantic_question(items, semantic)
    return merged, rejected, semantic.note


def _related_existing(
    plan: _DedupePlan,
    groups: list[_AnswerGroup],
    similarity: _SemanticSimilarity,
) -> list[_ExistingKnowledge]:
    """找出与这批候选问法相同或语义等价的已有知识条目。

    问法键完全相同直接命中；否则用问法阈值做语义比较（例如“保修期是多久”与“质保多长时间”）。
    语义比较不可用时只认问法键，因此不会误判。
    """
    related: list[_ExistingKnowledge] = []
    for group in groups:
        normalized = normalize_question(group.question)
        for item in plan.existing:
            if item.question_key == _question_group_key(group.question):
                related.append(item)
                continue
            if similarity.equivalent(normalized, normalize_question(item.question), threshold=similarity.question_threshold):
                related.append(item)
    return related


def _merge_by_semantic_question(
    items: list[_KnowledgeItem], similarity: _SemanticSimilarity
) -> list[_KnowledgeItem]:
    """把语义等价的问法合并成同一条知识。

    只有归一化问法完全相同才会落在同一个 `_KnowledgeItem` 里，因此这里处理的是“问法不同但同义”
    的情形（例如“保修期是多久”与“质保多长时间”）。合并后 questions 会带上全部真实问法，知识块
    只生成一条，Milvus 里也只有一条向量。

    **答案必须也语义等价才合并。** 两条知识的问法同义但答案不同时，它们描述的是不同结论（例如
    “退款多久到账”分别答 15 个工作日和 7 个工作日），合并会丢掉其中一个结论，而且给同一条知识
    挂上互相矛盾的两个问法。这种情况保持两条独立知识，让它们在检索时各自命中。

    合并按顺序进行且不设传递闭包：相似度是近似的，传递合并可能把“A 像 B、B 像 C、但 A 不像 C”的
    问法串成一条，导致检索语义漂移。这里以每条知识现有的问法集合作为比较基准，宁可少合并也不错并。
    """
    merged: list[_KnowledgeItem] = []
    for item in items:
        target: _KnowledgeItem | None = None
        for candidate in merged:
            if not _question_sets_equivalent(candidate.questions, item.questions, similarity):
                continue
            if not _answers_equivalent(candidate.answer, item.answer, similarity):
                # 问法同义但结论不同：不合并，保留为两条独立知识。
                continue
            target = candidate
            break
        if target is None:
            merged.append(item)
            continue
        # 语义等价才合并；代表答案取更长的一条，条目全部保留以便追溯来源消息。
        target.entries.extend(item.entries)
        target.answer = max(target.answer, item.answer, key=len)
        target.category = target.category or item.category
    return merged


def _question_sets_equivalent(
    left: list[str], right: list[str], similarity: _SemanticSimilarity
) -> bool:
    """判断两组问法是否互为同义：任一对问法语义等价即视为同一问法。"""
    for first in left:
        for second in right:
            if _question_group_key(first) == _question_group_key(second):
                return True
            if similarity.equivalent(
                normalize_question(first), normalize_question(second), threshold=similarity.question_threshold
            ):
                return True
    return False


def run_promotion(
    *,
    run_id: str,
    vectorize: bool = True,
    similarity: _SemanticSimilarity | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> PromotionRunResult:
    """执行整体去重与入库阶段。

    前置条件是所有待处理批次都已抽取完成：由 `run_pipeline` 用
    `staging_repo.has_unfinished_batches` 判定，判定不通过时根本不会走到这里。在部分候选上
    做去重会让跨批次的重复问法漏网，所以这个前置条件是硬性的。

    去重规则（详见 `dedupe_candidates`）：

    * 同一问法 + 同一答案 -> 合并成一条知识，questions 保存全部真实问法；
    * 同一问法与已有知识答案一致 -> 判为已入库，不重复生成正式知识与向量；
    * 同一问法不同答案（本轮内部或与已有知识冲突）-> 留在暂存表标为 rejected 并写明“留待人工
      确认”，不自动合并、不入库。

    入库复用 `knowledge_repo.upsert_source_chunks`，向量补齐复用 `indexing.vectorize_pending`；
    本模块没有第二套向量化逻辑。
    """
    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    if staging_repo.has_unfinished_batches():
        raise RuntimeError("仍有未抽取完成的批次，不能在部分候选上整体去重入库")

    staged = staging_repo.list_staged_candidates()
    if not staged:
        staging_repo.mark_extracted_batches_ready()
        batch_ids = staging_repo.list_ready_batch_ids()
        promoted_batch_ids = staging_repo.list_ready_batches_with_promoted_candidates()
        for batch_id in batch_ids:
            if batch_id in promoted_batch_ids:
                staging_repo.mark_batch_promoted(batch_id)
            else:
                staging_repo.mark_batch_skipped(batch_id, REASON_BATCH_NO_NEW_KNOWLEDGE)
        return PromotionRunResult(
            run_id=run_id, staged_candidates=0, distinct_questions=0, promoted_knowledge=0,
            merged_candidates=0, promoted_candidates=0, duplicate_candidates=0,
            conflict_candidates=0, batches_promoted=len(promoted_batch_ids), vectors_written=0,
        )

    plan = build_dedupe_plan(staged, staging_repo.list_existing_chunks())
    semantic = similarity or _new_similarity()
    items, rejected, downgrade_note = dedupe_candidates(plan, similarity=semantic)

    planned_promoted = {
        candidate.id
        for item in items
        for entry in item.entries
        for candidate in entry.candidates
    }
    planned_rejected = {candidate_id for candidate_id, _ in rejected}
    staged_ids = {candidate.id for candidate in staged}
    if planned_promoted & planned_rejected or (planned_promoted | planned_rejected) != staged_ids:
        raise RuntimeError("整体去重未给所有暂存候选唯一结论，停止入库")

    promoted_ids: list[str] = []
    chunk_id_by_candidate: dict[str, str] = {}
    merged_candidates = 0
    vectorize_requested = False
    for item in items:
        questions = sorted(item.questions)
        chunk_id, needs_vectorize = promote_group(
            questions=questions,
            answer=item.answer,
            category=item.category,
        )
        vectorize_requested = vectorize_requested or needs_vectorize
        for entry in item.entries:
            for candidate in entry.candidates:
                promoted_ids.append(candidate.id)
                chunk_id_by_candidate[candidate.id] = chunk_id
                merged_candidates += 1
        report(f"入库知识：{' / '.join(questions)[:60]}")

    # 按原因归类统计，便于交付说明“有多少候选留待处理”。
    duplicate_candidates = sum(1 for _, reason in rejected if reason == REASON_DUPLICATE_OF_EXISTING)
    conflict_candidates = sum(1 for _, reason in rejected if reason == REASON_ANSWER_CONFLICT)

    staging_repo.mark_candidates_decided(
        promoted_ids, rejected, chunk_id_by_candidate=chunk_id_by_candidate
    )
    staging_repo.mark_extracted_batches_ready()
    # 已写入候选结论但中断在 ready 的旧批次也在这里恢复；有正式知识的为 promoted，其余为 skipped。
    batch_ids = staging_repo.list_ready_batch_ids()
    batches_with_promoted_candidates = staging_repo.list_ready_batches_with_promoted_candidates()
    promoted_batch_ids = [batch_id for batch_id in batch_ids if batch_id in batches_with_promoted_candidates]
    skipped_batch_ids = [batch_id for batch_id in batch_ids if batch_id not in promoted_batch_ids]
    vectors_written = 0
    try:
        if vectorize and vectorize_requested:
            # 复用第一阶段的向量补齐流程：读 pending 块 -> BGE-M3 -> Milvus -> 回填状态。
            from . import indexing

            vectorize_result = indexing.vectorize_pending()
            vectors_written = vectorize_result.vectorized
    finally:
        # 批次推进放在 finally：知识已经写入 MySQL（权威原文已在库里），此时向量服务不可用
        # 不该让批次停在 ready，否则下一次运行会把同一批候选再走一次去重。
        # 未补齐的块保持 pending，可直接用建库命令的 vectorize 子命令补上。
        for batch_id in promoted_batch_ids:
            staging_repo.mark_batch_promoted(batch_id)
        # 没有任何新知识的批次标记跳过并推进续跑锚点；原因写入 error_message。
        for batch_id in skipped_batch_ids:
            staging_repo.mark_batch_skipped(batch_id, REASON_BATCH_NO_NEW_KNOWLEDGE)

    notes = [downgrade_note] if downgrade_note else []
    return PromotionRunResult(
        run_id=run_id,
        staged_candidates=len(staged),
        distinct_questions=plan.distinct_question_forms,
        promoted_knowledge=len(items),
        merged_candidates=merged_candidates,
        promoted_candidates=len(promoted_ids),
        duplicate_candidates=duplicate_candidates,
        conflict_candidates=conflict_candidates,
        batches_promoted=len(promoted_batch_ids),
        vectors_written=vectors_written,
        notes=notes,
    )


def promote_group(
    *,
    questions: list[str],
    answer: str,
    category: str,
) -> tuple[str, bool]:
    """把一组候选写成一条正式知识，返回 `(knowledge_chunks 主键, 是否需要补向量)`。

    只调用第一阶段的 `upsert_source_chunks`：`embedding_text` 用同一个 `build_embedding_text`，
    `chunk_key` 用同一个 `content_hash` 规则，因此向量文本格式、幂等键与文档/FAQ 来源完全一致，
    本模块没有第二套向量化逻辑。

    来源标识按规范化问法集合生成，`chunk_index` 恒为 0（每个来源就是一条问答知识）；问法集合不变
    时来源与 chunk_key 都不变，重复运行只会更新同一行，不会产生新知识或新向量。

    第二个返回值用于判断是否需要触发向量补齐：内容未变时 `upsert_source_chunks` 保持原状态
    （已向量化的块不会被打回 pending），此时不必调用向量服务，避免空跑。
    """
    source_id = _source_id_for_questions(questions)
    payload = {
        "chunk_key": _chunk_key(source_id, questions, answer),
        "chunk_index": 0,
        "category": category,
        "questions": questions,
        "answer": answer,
        "embedding_text": build_embedding_text(category, questions, answer),
        "embedding_fingerprint": embedding_fingerprint(),
        "section_path": category,
        "content_type": CONTENT_TYPE_MINED_QA,
        "is_key_clause": False,
        "needs_manual_review": False,
        "content_hash": content_hash(answer),
    }
    result = knowledge_repo.upsert_source_chunks(
        SOURCE_TYPE_CONVERSATION,
        source_id,
        [payload],
        source_path=None,
        source_title=questions[0] if questions else None,
    )
    # 用 upsert 返回的 chunk_key -> 主键映射回填候选，便于从知识反查抽取来源。
    chunk_id = result.chunk_ids.get(payload["chunk_key"], "")
    # 新插入的块或内容变化被重置为 pending 的块都需要补向量；原本已向量化的块不需要。
    needs_vectorize = result.inserted > 0 or result.reset_to_pending > 0
    return chunk_id, needs_vectorize


def _chunk_key(source_id: str, questions: list[str], answer: str) -> str:
    """生成知识块的稳定幂等键。

    与 `indexing._chunk_key` 同一规则（来源 + 序号 + 内容哈希），但内容哈希覆盖答案与全部问法：
    问法集合变了就应该是一条新知识（旧块被作废），而不是静默沿用旧 embedding_text。
    """
    content_digest = content_hash("\n".join([*sorted(questions), answer]))
    return sha256(f"{source_id}\n0\n{content_digest}".encode("utf-8")).hexdigest()


def run_pipeline(
    *,
    client: ConversationExtractionClient,
    vectorize: bool | None = None,
    promote: bool = True,
    on_progress: Callable[[str], None] | None = None,
) -> PipelineResult:
    """执行一轮任务：抽取 -> 暂存，完整时可整体去重 -> 入库 -> 补向量。

    这是定时任务每一轮调用的入口。返回结果里 `skipped_dedupe` 为真表示待处理池仍有批次没抽完
    （达到批次上限、部分批次失败或中断后遗留），去重推迟到下一轮，旧候选继续留在暂存池。
    """
    run_id = staging_repo.generate_run_id()
    extraction = run_extraction(run_id=run_id, client=client, on_progress=on_progress)
    notes: list[str] = []
    # 未认领的 pending 批次没有 run_id，失败批次也可能属于旧轮次；只检查本轮会漏掉它们。
    if staging_repo.has_unfinished_batches():
        notes.append(
            "本轮仍有未抽取完成的批次（达到批次上限或抽取失败），整体去重推迟到下一次运行；"
            "已抽取的候选保留在暂存表。"
        )
        return PipelineResult(extraction=extraction, skipped_dedupe=True, notes=notes)

    if not promote:
        notes.append("仅暂存模式：候选保留在暂存表，后续完整运行再整体去重并入库。")
        return PipelineResult(extraction=extraction, notes=notes)

    should_vectorize = mining_settings.vectorize_after_promote if vectorize is None else vectorize
    promotion = run_promotion(run_id=run_id, vectorize=should_vectorize, on_progress=on_progress)
    return PipelineResult(extraction=extraction, promotion=promotion, notes=notes)
