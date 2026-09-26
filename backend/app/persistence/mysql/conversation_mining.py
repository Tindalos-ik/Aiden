"""对话挖掘的 MySQL 读写：消息读取、批次进度、问答候选暂存。

与 `app.persistence.mysql.knowledge` 采用同一套约定：每个函数自行创建短生命周期 Session 并立即
关闭，绝不跨模型调用持有会话。抽取流程因此是“短事务读消息 -> 关闭会话 -> 调 LLM -> 短事务写
候选”，任何外部调用期间都没有打开的数据库连接。

**只读业务表。** 本模块只对 `conversations`、`messages` 做 SELECT，不改动任何业务数据；写入
只发生在 `conversation_mining_batches` 与 `conversation_qa_candidates` 两张新表。

**并发安全靠短事务认领。** 多个任务同时运行时，`claim_pending_batches` 在同一个短事务里
`SELECT ... FOR UPDATE SKIP LOCKED` 后立刻把状态置为 `extracting`：行锁保证同一批次只会被一个
运行拿到，`SKIP LOCKED` 让其他运行直接跳过被锁的行而不是排队等待。进程被杀会留下 `extracting`
行，由下一次运行的 `reset_stale_extracting_batches` 放回 `pending` 重跑。

**幂等靠确定性键。** 批次的 `batch_signature` 由会话与窗口内消息序列决定，候选的
`candidate_key` 由批次签名与候选序号决定；两者都是唯一键，因此重复运行只会命中已有行或跳过，
不会重复抽取同一批消息，也不会重复生成候选。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.persistence.mysql.database import get_session_factory
from app.persistence.mysql.models import (
    Conversation,
    ConversationMiningBatch,
    ConversationQaCandidate,
    KnowledgeChunk,
    Message,
    new_id,
    utc_now_naive,
)

# 批次状态常量；与 models.ConversationMiningBatch 的检查约束一致。
BATCH_STATUS_PENDING = "pending"
BATCH_STATUS_EXTRACTING = "extracting"
BATCH_STATUS_EXTRACTED = "extracted"
BATCH_STATUS_READY = "ready"
BATCH_STATUS_PROMOTED = "promoted"
BATCH_STATUS_SUPERSEDED = "superseded"
# 结构上无法抽取的窗口（单轮问答自身就超过模型可读长度），跳过但视为已处理，锚点照常推进。
BATCH_STATUS_SKIPPED = "skipped"
BATCH_STATUS_FAILED = "failed"

# 推进续跑锚点所依据的终态：只有确实得出结论的批次才能把锚点往前推。
# 注意这里**不包含** `superseded`——它表示窗口被显式作废，若拿它推进锚点，那段消息就再也读不到；
# 也不包含 `failed`，那是“下次要重试”。该状态目前不会有代码路径产生（批次签名由消息序列决定，
# 窗口变化只会生成新签名的新批次），保留它是为了让状态机语义完整。
BATCH_STATUS_ANCHOR_ADVANCING = (BATCH_STATUS_PROMOTED, BATCH_STATUS_SKIPPED)

# 候选状态常量；与 models.ConversationQaCandidate 的检查约束一致。
CANDIDATE_STATUS_STAGED = "staged"
CANDIDATE_STATUS_PROMOTED = "promoted"
CANDIDATE_STATUS_REJECTED = "rejected"

# 抽取只接受已完成的消息状态：streaming 尚未生成完、error 与 stopped 都不是可信答案。
TRUSTED_MESSAGE_STATUS = "complete"
# 可信答案的发送角色：助手自动回答与人工客服回答都可以作为知识依据。
ANSWER_ROLES = ("assistant", "staff")

# 单次查询读取的消息上限，防止单个超长会话一次把内存吃满。
MESSAGE_READ_LIMIT = 2000
# 认领批次时的单次批量上限。
CLAIM_BATCH_LIMIT = 20


@dataclass(frozen=True)
class MiningMessageRow:
    """待挖掘的一条消息；调用方拿到普通数据对象，不持有 ORM 对象或连接。"""

    message_id: str
    conversation_id: str
    sender_role: str
    content: str
    created_at: datetime
    status: str


@dataclass(frozen=True)
class MiningBatchRow:
    """一个抽取批次的可读快照，供抽取与去重编排使用。"""

    id: str
    batch_signature: str
    conversation_id: str
    run_id: str
    status: str
    first_message_at: datetime
    last_message_at: datetime
    message_ids: list[str]
    turn_count: int


@dataclass(frozen=True)
class CandidateDraft:
    """一条待写入暂存表的候选。

    `status` 为 `staged` 表示通过校验、可参与去重；为 `rejected` 时 `rejection_reason` 必须给出
    原因，说明它为什么留在暂存表而不进入正式知识库。
    """

    batch_id: str
    batch_signature: str
    conversation_id: str
    candidate_key: str
    question: str
    answer: str
    category: str
    source_user_message_id: str
    source_answer_message_id: str
    evidence: str
    confidence: float | None
    status: str
    rejection_reason: str | None
    answer_fingerprint: str


@dataclass(frozen=True)
class StagedCandidate:
    """读取回来的暂存候选，整体去重的输入。"""

    id: str
    batch_id: str
    conversation_id: str
    question: str
    answer: str
    category: str
    answer_fingerprint: str
    evidence: str
    source_user_message_id: str
    source_answer_message_id: str
    confidence: float | None


@dataclass(frozen=True)
class ExistingChunk:
    """已有正式知识块的摘要，用于去重时判断同一问法是否已经入库。"""

    id: str
    chunk_key: str
    source_id: str
    category: str
    questions: list[str]
    answer: str


def _normalized_identity(text: str) -> str:
    """归一化文本后再计算指纹：去掉空白与全角空格，避免排版差异把同一答案判成不同答案。"""
    return "".join(text.split()).replace("\u3000", "")


def answer_fingerprint(answer: str) -> str:
    """答案的稳定指纹，用于跨批次、跨来源判断“答案是否一致”。"""
    return sha256(_normalized_identity(answer).encode("utf-8")).hexdigest()


def answer_text_equal(left: str, right: str) -> bool:
    """判断两条答案在规范化后是否完全相同。

    这是最快也最确定的“同一答案”判定；只有它不成立时才需要回落成语义比较。放在持久化层是为了
    让指纹定义与比较口径共用一处实现，避免两处归一化规则漂移。
    """
    return answer_fingerprint(left) == answer_fingerprint(right)


def batch_signature(conversation_id: str, message_ids: list[str], roles: list[str]) -> str:
    """计算抽取窗口的稳定签名。

    签名覆盖会话、窗口内消息主键与发送角色序列：消息 id 不变时重跑得到同一签名（幂等跳过），
    窗口内补进新消息或边界变化时会得到新签名，从而生成一个新批次。消息顺序参与签名，因此
    “同一条消息换个位置”也会被识别为不同窗口，而不是静默复用旧候选。

    角色参与签名是因为同一批消息里用户消息与答案消息的配对关系决定抽取内容；把答案消息的
    状态从未完成改成完成后，角色序列不变但内容变了，那种情况由 `message_ids` 中新增的
    助手消息 id 覆盖。
    """
    digest = sha256()
    digest.update(conversation_id.encode("utf-8"))
    for message_id, role in zip(message_ids, roles):
        digest.update(b"\x00")
        digest.update(message_id.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(role.encode("utf-8"))
    return digest.hexdigest()


def candidate_key(batch_signature_value: str, index: int) -> str:
    """候选的稳定幂等键：批次签名 + 候选在该批次输出中的序号。"""
    return sha256(f"{batch_signature_value}\n{index}".encode("utf-8")).hexdigest()


def _to_message_row(message: Message) -> MiningMessageRow:
    """把 ORM 消息转成普通数据对象，避免 ORM 实例离开 Session。"""
    return MiningMessageRow(
        message_id=message.id,
        conversation_id=message.conversation_id,
        sender_role=message.sender_role,
        content=message.content,
        created_at=message.created_at,
        status=message.status,
    )


def list_conversations_with_trusted_messages(limit: int) -> list[str]:
    """列出锚点之后仍有可信答案的会话 id，按答案时间升序排列。

    已处理完的会话必须退出前 `limit` 名额，否则会话上限较小时，后面的会话会被永久饿死。
    只看可信答案，末尾尚未得到答复的用户消息留待下一轮有答案后再处理。
    """
    factory = get_session_factory()
    with factory() as session:
        anchors = (
            select(
                ConversationMiningBatch.conversation_id.label("conversation_id"),
                func.max(ConversationMiningBatch.last_message_at).label("last_message_at"),
            )
            .where(ConversationMiningBatch.status.in_(BATCH_STATUS_ANCHOR_ADVANCING))
            .group_by(ConversationMiningBatch.conversation_id)
            .subquery()
        )
        rows = session.execute(
            select(Message.conversation_id, func.max(Message.created_at).label("last_at"))
            .outerjoin(anchors, anchors.c.conversation_id == Message.conversation_id)
            .where(
                Message.status == TRUSTED_MESSAGE_STATUS,
                Message.sender_role.in_(ANSWER_ROLES),
                or_(anchors.c.last_message_at.is_(None), Message.created_at > anchors.c.last_message_at),
            )
            .group_by(Message.conversation_id)
            .order_by(func.max(Message.created_at).asc(), Message.conversation_id.asc())
            .limit(limit)
        )
        return [conversation_id for conversation_id, _ in rows]


def list_trusted_messages(
    conversation_id: str, *, after: datetime | None = None, limit: int = MESSAGE_READ_LIMIT
) -> list[MiningMessageRow]:
    """按稳定顺序读取一个会话中的可信消息。

    顺序是 `(created_at, id)` 升序，与消息表索引一致，因此同一批数据的顺序在每次运行中都相同。
    只读取 `complete` 状态的消息：`streaming` 还在生成、`error` 与 `stopped` 都不是可信答案，
    把它们当依据会把半截回答变成“知识”。

    `after` 是续跑锚点（不含），由上一次已入库批次的时间给出；为空表示从头读取。
    """
    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.status == TRUSTED_MESSAGE_STATUS,
                Message.sender_role.in_(("user",) + ANSWER_ROLES),
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(limit)
        )
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        rows = list(session.scalars(stmt))
        return [_to_message_row(message) for message in rows]


def get_conversation_user_id(conversation_id: str) -> str | None:
    """读取会话归属用户；会话不存在时返回 None。

    会话归属是知识来源的一部分（同一个人在同一会话里的多轮追问需要合并成一个上下文），但正式
    知识里不保存用户标识——用户身份只在抽取时用于理解对话，不写入暂存表与知识库。
    """
    factory = get_session_factory()
    with factory() as session:
        return session.scalar(
            select(Conversation.user_id).where(Conversation.id == conversation_id)
        )


def last_terminal_message_at(conversation_id: str) -> datetime | None:
    """读取某会话已处理批次的最后消息时间，作为续跑锚点。

    统计 `promoted` 与 `skipped` 两类终态：前者已产生正式知识，后者是结构上无法抽取的窗口
    （单轮问答超过模型可读长度）。两者都必须推进锚点，否则每次运行都会重新读取同一段历史并
    重建同一个无法处理的批次。

    被取代（`superseded`）或失败（`failed`）的批次不计入：它们没有产生结论，若用来推进锚点，
    那部分消息会被永久跳过。
    """
    factory = get_session_factory()
    with factory() as session:
        return session.scalar(
            select(func.max(ConversationMiningBatch.last_message_at)).where(
                ConversationMiningBatch.conversation_id == conversation_id,
                ConversationMiningBatch.status.in_(BATCH_STATUS_ANCHOR_ADVANCING),
            )
        )


def create_batch_if_absent(
    *,
    conversation_id: str,
    signature: str,
    message_ids: list[str],
    first_message_at: datetime,
    last_message_at: datetime,
    turn_count: int,
) -> str | None:
    """按签名创建一个待抽取批次；签名已存在（含并发创建）时返回 None。

    唯一键冲突是这里的正常分支而不是错误：并发运行可能同时算出同一个窗口，先提交者胜出，后者
    直接跳过。用嵌套事务（SAVEPOINT）吸收冲突，外层事务继续可用。

    `run_id` 在认领时写入而不是这里：只有真正被调用了模型的批次才属于本轮，这样“本轮抽取出的
    候选”与“本轮实际处理过的窗口”始终保持一致。
    """
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            batch = ConversationMiningBatch(
                conversation_id=conversation_id,
                batch_signature=signature,
                run_id=None,
                claimed_by=None,
                status=BATCH_STATUS_PENDING,
                first_message_at=first_message_at,
                last_message_at=last_message_at,
                message_ids=list(message_ids),
                turn_count=turn_count,
                candidate_count=0,
            )
            try:
                with session.begin_nested():
                    session.add(batch)
                return batch.id
            except IntegrityError:
                return None


def claim_pending_batches(run_id: str, *, limit: int = CLAIM_BATCH_LIMIT) -> list[MiningBatchRow]:
    """认领待抽取批次并立即置为 `extracting`，返回被本次运行独占的批次。

    同一事务内先 `FOR UPDATE SKIP LOCKED` 再更新状态：行锁在事务结束前一直持有，其他运行的
    SELECT 会跳过这些行，因此同一批次不会被两个运行同时抽取。`SKIP LOCKED` 而不是等待，是为了
    让并发运行各干各的而不是串行排队。

    `failed` 状态的批次由下一轮重新认领；同一轮失败后不会反复认领并耗尽批次预算。
    错误信息在下次认领时覆盖。
    """
    factory = get_session_factory()
    now = utc_now_naive()
    claimed: list[MiningBatchRow] = []
    with factory() as session:
        with session.begin():
            rows = list(
                session.scalars(
                    select(ConversationMiningBatch)
                    .where(
                        or_(
                            ConversationMiningBatch.status == BATCH_STATUS_PENDING,
                            and_(
                                ConversationMiningBatch.status == BATCH_STATUS_FAILED,
                                or_(
                                    ConversationMiningBatch.run_id.is_(None),
                                    ConversationMiningBatch.run_id != run_id,
                                ),
                            ),
                        )
                    )
                    .order_by(
                        ConversationMiningBatch.first_message_at.asc(),
                        ConversationMiningBatch.id.asc(),
                    )
                    .limit(max(1, limit))
                    .with_for_update(skip_locked=True)
                )
            )
            for batch in rows:
                batch.status = BATCH_STATUS_EXTRACTING
                batch.claimed_by = run_id
                batch.run_id = run_id
                batch.error_message = None
                batch.updated_at = now
                claimed.append(_to_batch_row(batch))
    return claimed


def _to_batch_row(batch: ConversationMiningBatch) -> MiningBatchRow:
    """把 ORM 批次转成普通数据对象，允许在 Session 关闭后继续使用。"""
    return MiningBatchRow(
        id=batch.id,
        batch_signature=batch.batch_signature,
        conversation_id=batch.conversation_id,
        run_id=batch.run_id,
        status=batch.status,
        first_message_at=batch.first_message_at,
        last_message_at=batch.last_message_at,
        message_ids=list(batch.message_ids or []),
        turn_count=batch.turn_count,
    )


def reset_stale_extracting_batches() -> int:
    """把遗留的 `extracting` 批次放回 `pending`，返回重置条数。

    任务被强杀时，已认领但没写回状态的批次会停在 `extracting`；下一次运行的起始处调用本函数，
    使它们重新可被认领。因为同一时刻只允许一个任务实例运行（入口用非阻塞锁保证），这里不需要
    区分“别的实例正在抽取”这种情况。
    """
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            result = session.execute(
                update(ConversationMiningBatch)
                .where(ConversationMiningBatch.status == BATCH_STATUS_EXTRACTING)
                .values(status=BATCH_STATUS_PENDING, claimed_by=None)
            )
        return result.rowcount or 0


def mark_batch_extracted(batch_id: str, *, candidate_count: int) -> None:
    """把批次标记为已抽取，并记录写入的候选条数。"""
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            session.execute(
                update(ConversationMiningBatch)
                .where(ConversationMiningBatch.id == batch_id)
                .values(
                    status=BATCH_STATUS_EXTRACTED,
                    candidate_count=candidate_count,
                    extracted_at=utc_now_naive(),
                    error_message=None,
                )
            )


def mark_batch_failed(batch_id: str, error_message: str) -> None:
    """把批次标记为失败并保留错误信息；错误信息截断到列长度以内。"""
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            session.execute(
                update(ConversationMiningBatch)
                .where(ConversationMiningBatch.id == batch_id)
                .values(
                    status=BATCH_STATUS_FAILED,
                    claimed_by=None,
                    error_message=(error_message or "")[:500],
                )
            )


def mark_batch_skipped(batch_id: str, reason: str) -> None:
    """把批次标记为结构上不可抽取并记录原因。

    与 `failed` 的区别是语义：`failed` 表示这次调用没成功、下次要重试；`skipped` 表示无论重试
    多少次都抽不出来（单轮问答自身超过模型可读长度），因此它计入已处理窗口并让续跑锚点前进，
    避免同一段超长历史被反复读取。原因保留在 `error_message` 里供人工拆分后重跑。
    """
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            session.execute(
                update(ConversationMiningBatch)
                .where(ConversationMiningBatch.id == batch_id)
                .values(
                    status=BATCH_STATUS_SKIPPED,
                    claimed_by=None,
                    error_message=(reason or "")[:500],
                )
            )


def upsert_candidates(drafts: list[CandidateDraft]) -> int:
    """以 `candidate_key` 为幂等键写入候选，返回实际新增或更新的行数。

    重复抽取同一批次时，候选键相同则更新内容与状态，不新增行；这样“重跑”既不会产生重复暂存
    记录，也能刷新被拒原因（例如调整校验规则后原因变化）。
    """
    if not drafts:
        return 0
    factory = get_session_factory()
    now = utc_now_naive()
    written = 0
    with factory() as session:
        with session.begin():
            existing = {
                row.candidate_key: row
                for row in session.scalars(
                    select(ConversationQaCandidate).where(
                        ConversationQaCandidate.candidate_key.in_(
                            [draft.candidate_key for draft in drafts]
                        )
                    )
                )
            }
            for draft in drafts:
                current = existing.get(draft.candidate_key)
                if current is None:
                    session.add(
                        ConversationQaCandidate(
                            batch_id=draft.batch_id,
                            conversation_id=draft.conversation_id,
                            candidate_key=draft.candidate_key,
                            question=draft.question,
                            answer=draft.answer,
                            category=draft.category,
                            source_user_message_id=draft.source_user_message_id,
                            source_answer_message_id=draft.source_answer_message_id,
                            evidence=draft.evidence,
                            confidence=draft.confidence,
                            status=draft.status,
                            rejection_reason=draft.rejection_reason,
                            answer_fingerprint=draft.answer_fingerprint,
                        )
                    )
                else:
                    current.question = draft.question
                    current.answer = draft.answer
                    current.category = draft.category
                    current.evidence = draft.evidence
                    current.confidence = draft.confidence
                    current.status = draft.status
                    current.rejection_reason = draft.rejection_reason
                    current.answer_fingerprint = draft.answer_fingerprint
                    current.batch_id = draft.batch_id
                    current.updated_at = now
                written += 1
        return written


def list_staged_candidates() -> list[StagedCandidate]:
    """读取所有已抽取或待入库批次的待去重候选，包含以前运行留下的候选。

    `run_id` 只用于追溯抽取轮次，不能限定整体去重范围。一次运行因失败或上限暂停后，已抽取
    批次仍带旧 `run_id`；续跑必须将其与新批次一起去重。这里不截断结果，避免遗漏尾部候选。
    """
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(ConversationQaCandidate, ConversationMiningBatch.batch_signature)
            .join(
                ConversationMiningBatch,
                ConversationMiningBatch.id == ConversationQaCandidate.batch_id,
            )
            .where(
                ConversationMiningBatch.status.in_((BATCH_STATUS_EXTRACTED, BATCH_STATUS_READY)),
                ConversationQaCandidate.status == CANDIDATE_STATUS_STAGED,
            )
            .order_by(
                ConversationQaCandidate.conversation_id.asc(),
                ConversationQaCandidate.created_at.asc(),
                ConversationQaCandidate.id.asc(),
            )
        )
        return [
            StagedCandidate(
                id=candidate.id,
                batch_id=candidate.batch_id,
                conversation_id=candidate.conversation_id,
                question=candidate.question,
                answer=candidate.answer,
                category=candidate.category,
                answer_fingerprint=candidate.answer_fingerprint,
                evidence=candidate.evidence,
                source_user_message_id=candidate.source_user_message_id,
                source_answer_message_id=candidate.source_answer_message_id,
                confidence=float(candidate.confidence) if candidate.confidence is not None else None,
            )
            for candidate, _ in rows
        ]


def has_unfinished_batches() -> bool:
    """判断整个待处理批次池是否还有没抽取完的批次。

    `pending` 可能尚未被任何运行认领，因此 `run_id` 为空；`failed` 也可能属于旧运行。
    两者均须阻止整体去重，直到对应候选进入同一个去重池。
    """
    factory = get_session_factory()
    with factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ConversationMiningBatch)
            .where(
                ConversationMiningBatch.status.in_(
                    (BATCH_STATUS_PENDING, BATCH_STATUS_EXTRACTING, BATCH_STATUS_FAILED)
                ),
            )
        )
        return bool(count)


def mark_extracted_batches_ready() -> int:
    """把所有轮次已抽取的批次置为 `ready`，返回条数。"""
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            result = session.execute(
                update(ConversationMiningBatch)
                .where(
                    ConversationMiningBatch.status == BATCH_STATUS_EXTRACTED,
                )
                .values(status=BATCH_STATUS_READY)
            )
        return result.rowcount or 0


def mark_batch_promoted(batch_id: str) -> None:
    """把批次标记为已入库；这同时是续跑锚点向前推进的依据。"""
    factory = get_session_factory()
    with factory() as session:
        with session.begin():
            session.execute(
                update(ConversationMiningBatch)
                .where(ConversationMiningBatch.id == batch_id)
                .values(
                    status=BATCH_STATUS_PROMOTED,
                    claimed_by=None,
                    promoted_at=utc_now_naive(),
                )
            )


def mark_candidates_decided(
    promoted_ids: list[str],
    rejected: list[tuple[str, str]],
    *,
    chunk_id_by_candidate: dict[str, str] | None = None,
) -> None:
    """一次性写回去重结论：候选置为已入库或已拒绝，并记录并入的正式知识块。

    `rejected` 的元素是 (候选主键, 拒绝原因)；`chunk_id_by_candidate` 把入库候选映射到最终
    生成的 knowledge_chunks 主键，用于从知识反查抽取来源。
    """
    if not promoted_ids and not rejected:
        return
    factory = get_session_factory()
    now = utc_now_naive()
    mapping = chunk_id_by_candidate or {}
    with factory() as session:
        with session.begin():
            for candidate_id in promoted_ids:
                session.execute(
                    update(ConversationQaCandidate)
                    .where(ConversationQaCandidate.id == candidate_id)
                    .values(
                        status=CANDIDATE_STATUS_PROMOTED,
                        rejection_reason=None,
                        deduped_at=now,
                        promoted_chunk_id=mapping.get(candidate_id),
                    )
                )
            for candidate_id, reason in rejected:
                session.execute(
                    update(ConversationQaCandidate)
                    .where(ConversationQaCandidate.id == candidate_id)
                    .values(
                        status=CANDIDATE_STATUS_REJECTED,
                        rejection_reason=(reason or "")[:500],
                        deduped_at=now,
                    )
                )


def list_existing_chunks() -> list[ExistingChunk]:
    """读取已有正式知识块，供去重时判断“这个问法/答案是不是已经入库了”。

    只读未作废的块（`vectorized`、`pending`、`need_manual_review`）：`superseded` 的块已经无效，
    拿它参与去重会让本该入库的新知识被误判为重复。不能截断已有知识，否则尾部知识
    无法参与去重，续跑可能生成重复块。
    """
    factory = get_session_factory()
    with factory() as session:
        rows = session.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.vector_status != "superseded")
            .order_by(KnowledgeChunk.source_type.asc(), KnowledgeChunk.source_id.asc())
        )
        return [
            ExistingChunk(
                id=chunk.id,
                chunk_key=chunk.chunk_key,
                source_id=chunk.source_id,
                category=chunk.category,
                questions=list(chunk.questions or []),
                answer=chunk.answer,
            )
            for chunk in rows
        ]


def list_ready_batch_ids() -> list[str]:
    """列出所有等待入库结论的批次主键，包含之前运行留下的 `ready`。"""
    factory = get_session_factory()
    with factory() as session:
        rows = session.scalars(
            select(ConversationMiningBatch.id).where(
                ConversationMiningBatch.status == BATCH_STATUS_READY,
            )
        )
        return list(rows)


def list_ready_batches_with_promoted_candidates() -> set[str]:
    """找出已有正式知识结论的 ready 批次，用于恢复候选已决但批次未终结的中断。"""
    factory = get_session_factory()
    with factory() as session:
        return set(
            session.scalars(
                select(ConversationQaCandidate.batch_id)
                .join(ConversationMiningBatch, ConversationMiningBatch.id == ConversationQaCandidate.batch_id)
                .where(
                    ConversationMiningBatch.status == BATCH_STATUS_READY,
                    ConversationQaCandidate.status == CANDIDATE_STATUS_PROMOTED,
                )
                .distinct()
            )
        )


def latest_run_id() -> str | None:
    """读取最近一次真正调用过模型的运行标识，供手动续跑指定运行。

    只看 `run_id` 非空的批次：待抽取批次的 `run_id` 为空，不代表任何一轮处理。
    """
    factory = get_session_factory()
    with factory() as session:
        return session.scalar(
            select(ConversationMiningBatch.run_id)
            .where(ConversationMiningBatch.run_id.is_not(None))
            .order_by(ConversationMiningBatch.updated_at.desc(), ConversationMiningBatch.id.desc())
            .limit(1)
        )


def count_batches_by_status() -> dict[str, int]:
    """统计各批次状态的数量，供任务结束时输出进度。"""
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(ConversationMiningBatch.status, func.count()).group_by(
                ConversationMiningBatch.status
            )
        )
        return {status: count for status, count in rows}


def count_candidates_by_status() -> dict[str, int]:
    """统计各候选状态的数量，供判断还有多少候选留待处理。"""
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(ConversationQaCandidate.status, func.count()).group_by(
                ConversationQaCandidate.status
            )
        )
        return {status: count for status, count in rows}


def count_run_candidates_by_status(run_id: str) -> dict[str, int]:
    """统计某次运行的候选在各状态下的数量，用于运行结束时的自检输出。"""
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(ConversationQaCandidate.status, func.count())
            .join(
                ConversationMiningBatch,
                ConversationMiningBatch.id == ConversationQaCandidate.batch_id,
            )
            .where(ConversationMiningBatch.run_id == run_id)
            .group_by(ConversationQaCandidate.status)
        )
        return {status: count for status, count in rows}


def generate_run_id() -> str:
    """生成运行标识，供编排层在开始一轮处理时使用。"""
    return new_id()


def list_candidates_for_review(
    *, status: str = CANDIDATE_STATUS_REJECTED, limit: int = 50, run_id: str | None = None
) -> list[dict]:
    """列出候选及其拒绝原因，供人工复核抽取质量。

    只读接口。返回字典序列而不是 ORM 对象，调用方在查询结束后不再持有 Session。来源消息标识
    一并返回，便于从候选直接回到对话原文核对依据。
    """
    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(ConversationQaCandidate, ConversationMiningBatch.batch_signature)
            .join(
                ConversationMiningBatch,
                ConversationMiningBatch.id == ConversationQaCandidate.batch_id,
            )
            .where(ConversationQaCandidate.status == status)
            .order_by(
                ConversationQaCandidate.conversation_id.asc(),
                ConversationQaCandidate.created_at.asc(),
                ConversationQaCandidate.id.asc(),
            )
            .limit(max(1, limit))
        )
        if run_id:
            stmt = stmt.where(ConversationMiningBatch.run_id == run_id)
        return [
            {
                "candidate_id": candidate.id,
                "conversation_id": candidate.conversation_id,
                "batch_signature": signature[:12],
                "question": candidate.question,
                "answer": candidate.answer,
                "category": candidate.category,
                "status": candidate.status,
                "rejection_reason": candidate.rejection_reason,
                "source_user_message_id": candidate.source_user_message_id,
                "source_answer_message_id": candidate.source_answer_message_id,
                "promoted_chunk_id": candidate.promoted_chunk_id,
            }
            for candidate, signature in session.execute(stmt)
        ]
