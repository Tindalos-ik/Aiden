"""结构感知切分：把章节内容切成可直接向量化、且能独立理解的知识块。

切分遵循三条硬约束：

1. **不输出半截句子。** 所有文本切分都发生在完整句子边界上；相邻块的重叠也只从上一块
   尾部按完整句子回退取得。
2. **超长章节递归下钻。** 子标题是天然语义边界，章节超预算时先递归切子章节；没有子标题
   可下钻或下钻层数用尽后，再按句子打包。
3. **异常长句显式标记。** 单句本身超过 `RAG_MAX_SENTENCE_TOKENS` 时，整句原样保留并标记
   `needs_manual_review`，由导入流程排除在向量化之外，而不是静默截断。

表格按行分块，每块都会复制表头，使每一块脱离上下文也能独立理解。长度全部以
`app.services.rag.length` 的 token 计量为准。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.config.rag import rag_settings

from .length import LengthMeter, get_length_meter, longest_sentence_tokens, split_sentences

ContentType = Literal["text", "table", "code", "mixed"]

# 段落块的类型：正文、整块 Markdown 表格、围栏代码块。
BlockType = Literal["text", "table", "code"]

_MANUAL_REVIEW_SENTENCE = "单句长度超过单句上限，需人工拆分后再向量化"
_MANUAL_REVIEW_SENTENCE_BUDGET = "单句长度超过单块预算，需人工拆分后再向量化"
_MANUAL_REVIEW_TABLE_ROW = "表格单行宽度超过单块预算，需人工拆分后再向量化"
_MANUAL_REVIEW_TABLE_HEADER = "表格表头宽度超过单块预算，需人工拆分后再向量化"
_MANUAL_REVIEW_CODE = "代码块长度超过单块预算，需人工拆分后再向量化"

# 关键条款判定用的标题关键词：退货、售后这类条款参与检索排序加权。
# 与 app.services.rag.knowledge 中的同名常量保持一致；因为 chunking 不能反向导入 knowledge
# （knowledge 依赖 chunking 的数据结构），这里单独定义并由切分层直接标注到块上。
_KEY_CLAUSE_KEYWORDS = (
    "退货",
    "退款",
    "换货",
    "售后",
    "保修",
    "质保",
    "赔付",
    "运费险",
    "发票",
    "退换",
)


def _section_has_special_block(section: SectionData) -> bool:
    """判断章节直属内容里是否有表格或代码块。

    这类块不能按纯文本整段处理：表格需要标记 content_type 并按行独立理解，代码块一旦被
    句子切分会破坏语义，因此含它们的章节一律走单位打包分支。
    """
    return any(block.type != "text" for block in section.blocks)


@dataclass(frozen=True)
class Block:
    """章节内的一个内容块；表格与代码块保留原始 Markdown 文本。"""

    type: BlockType
    text: str


@dataclass(frozen=True)
class SectionData:
    """一个标题及其直属内容，children 是更深层级的子章节。

    没有标题的文档开头内容也会生成一个 `title=""` 的根章节，此时 section_path 为 ""，
    该内容只作为文档前沿信息存在，不进入向量化（见 `app.services.rag.knowledge` 的过滤规则）。
    """

    title: str
    level: int
    path: tuple[str, ...]
    blocks: tuple[Block, ...] = ()
    children: tuple["SectionData", ...] = ()


@dataclass
class ChunkDraft:
    """切分产出的一块知识；此时还没有落到 MySQL 的 id。

    `content`、`content_type`、`section_path` 和待人工处理标记由切分层填写；`questions` 与
    `is_key_clause` 也由切分层按“产出该块的章节”填写——递归下钻时块来自更深的子章节，只有
    切分过程知道它的直接归属；`category` 则由上层 `app.services.rag.knowledge` 按来源补齐。

    `needs_manual_review` 为真表示这块不能安全向量化（异常长句或超宽表格行），导入流程会
    把它写入 MySQL 并标为 `need_manual_review`，跳过 Milvus。

    `section_path` 在打包时留空，由 `chunk_section` 在返回前统一写入：打包函数只关心长度与
    句子边界，不需要知道自己在哪个章节里。
    """

    content: str
    content_type: ContentType
    section_path: str
    needs_manual_review: bool = False
    review_reason: str | None = None
    category: str = ""
    questions: tuple[str, ...] = ()
    is_key_clause: bool = False


@dataclass
class _SentenceUnit:
    """打包用的最小单位：完整句子、一张已分块的表格或一个代码块。"""

    text: str
    content_type: Literal["text", "table", "code"]
    needs_manual_review: bool = False
    review_reason: str | None = None


@dataclass
class _ChunkAccumulator:
    """打包过程中的可变状态；仅内部使用。

    `new_units` 记录当前块里“本块新增”的单位数（不含从上一块搬来的重叠前缀）：只有重叠、
    没有新增内容的块不应该被输出，否则会在输入本身重复时产出内容完全相同的块。
    """

    units: list[_SentenceUnit] = field(default_factory=list)
    new_units: int = 0

    def content(self) -> str:
        return "\n".join(unit.text for unit in self.units)

    def project_content(self, unit: _SentenceUnit) -> str:
        """返回加入单位后的真实文本，供精确分词器按最终内容计量。"""
        if not self.units:
            return unit.text
        return f"{self.content()}\n{unit.text}"

    def add(self, unit: _SentenceUnit) -> None:
        """把单位并入本块。"""
        self.units.append(unit)

    def content_type(self) -> ContentType:
        kinds = {unit.content_type for unit in self.units}
        if not kinds:
            return "text"
        if len(kinds) == 1:
            return next(iter(kinds))
        return "mixed"

    def needs_manual_review(self) -> bool:
        return any(unit.needs_manual_review for unit in self.units)

    def review_reason(self) -> str | None:
        for unit in self.units:
            if unit.review_reason:
                return unit.review_reason
        return None


def split_markdown_table(table: str, meter: LengthMeter | None = None) -> list[_SentenceUnit]:
    """把一张 Markdown 表格按数据行分块，每块复制表头。

    表头是表格语义的一部分：拆开后每块都真实包含表头行，因此单块送进向量化后依然能独立
    理解。行数上限由 `RAG_TABLE_ROWS_PER_CHUNK` 控制；行数不多但整表仍超预算时按预算装箱。
    单行本身就超过预算时，整行独立成块并标记待人工处理。
    """
    active_meter = meter or get_length_meter()
    rows = [row for row in (line.rstrip() for line in table.splitlines()) if row.strip()]
    if len(rows) < 2:
        # 不是标准表格（至少需要表头与分隔行），按普通段落处理。
        text = "\n".join(rows)
        return [_SentenceUnit(text=text, content_type="text")] if text else []

    header_rows = rows[:2]
    header_text = "\n".join(header_rows)
    data_rows = rows[2:]
    budget = rag_settings.chunk_budget_tokens
    if not data_rows:
        too_wide = active_meter.estimate_tokens(header_text) > budget
        return [
            _SentenceUnit(
                text=header_text,
                content_type="table",
                needs_manual_review=too_wide,
                review_reason=_MANUAL_REVIEW_TABLE_HEADER if too_wide else None,
            )
        ]

    row_limit = max(1, rag_settings.table_rows_per_chunk)
    units: list[_SentenceUnit] = []
    current_rows: list[str] = []

    def flush() -> None:
        """把当前累计的数据行连同表头输出为独立表格块，并重置累计。"""
        nonlocal current_rows
        if not current_rows:
            return
        units.append(
            _SentenceUnit(
                text="\n".join([*header_rows, *current_rows]),
                content_type="table",
            )
        )
        current_rows = []

    for row in data_rows:
        single_row_text = f"{header_text}\n{row}"
        if active_meter.estimate_tokens(single_row_text) > budget:
            # 单行加表头已超预算：无法在保住完整行的前提下安全向量化。
            flush()
            units.append(
                _SentenceUnit(
                    text=single_row_text,
                    content_type="table",
                    needs_manual_review=True,
                    review_reason=_MANUAL_REVIEW_TABLE_ROW,
                )
            )
            continue
        candidate = "\n".join([*header_rows, *current_rows, row])
        if current_rows and (
            len(current_rows) >= row_limit or active_meter.estimate_tokens(candidate) > budget
        ):
            flush()
        current_rows.append(row)
    flush()
    return units


def _sentence_units(text: str, meter: LengthMeter) -> list[_SentenceUnit]:
    """正文一律以完整句子为单位，供装箱和重叠共同使用同一组边界。"""
    units: list[_SentenceUnit] = []
    for sentence in split_sentences(text):
        sentence_tokens = meter.estimate_tokens(sentence)
        exceeds_sentence_limit = sentence_tokens > rag_settings.effective_max_sentence_tokens
        exceeds_chunk_budget = sentence_tokens > rag_settings.chunk_budget_tokens
        reason = None
        if exceeds_sentence_limit:
            reason = _MANUAL_REVIEW_SENTENCE
        elif exceeds_chunk_budget:
            reason = _MANUAL_REVIEW_SENTENCE_BUDGET
        units.append(
            _SentenceUnit(
                text=sentence,
                content_type="text",
                needs_manual_review=exceeds_sentence_limit or exceeds_chunk_budget,
                review_reason=reason,
            )
        )
    return units


def _content_units(section: SectionData, meter: LengthMeter) -> list[_SentenceUnit]:
    """按原始顺序把章节直属内容展开成可打包的单位序列。

    表格被替换为按行分好的多个表格块，正文与代码块保持原有先后关系；代码块整体不切开，
    因为切断代码会破坏语义。
    """
    units: list[_SentenceUnit] = []
    for block in section.blocks:
        if block.type == "table":
            units.extend(split_markdown_table(block.text, meter))
        elif block.type == "code":
            too_long = meter.estimate_tokens(block.text) > rag_settings.chunk_budget_tokens
            units.append(
                _SentenceUnit(
                    text=block.text,
                    content_type="code",
                    needs_manual_review=too_long,
                    review_reason=_MANUAL_REVIEW_CODE if too_long else None,
                )
            )
        else:
            units.extend(_sentence_units(block.text, meter))
    return units


def _pack_units(units: list[_SentenceUnit], meter: LengthMeter) -> list[ChunkDraft]:
    """把单位序列按预算装箱成块，并在相邻块之间加入完整句子的重叠。

    重叠只取上一块新写入的尾部完整句子；同时用实际拼接文本检查重叠预算和新块预算。
    句子太长或放不下下一句时省略重叠，不复制半句，也不为重叠预留固定空间。
    """
    drafts: list[ChunkDraft] = []
    accumulator = _ChunkAccumulator()
    budget = rag_settings.chunk_budget_tokens
    overlap_budget = max(rag_settings.overlap_tokens, 0)

    def overlap_units(source: _ChunkAccumulator, next_unit: _SentenceUnit) -> list[_SentenceUnit]:
        """从已输出块的新增句子末尾回退，严格满足两项预算。"""
        overlap: list[_SentenceUnit] = []
        if not overlap_budget or source.new_units <= 0 or next_unit.content_type != "text":
            return overlap
        for unit in reversed(source.units[-source.new_units :]):
            if unit.content_type != "text" or unit.needs_manual_review:
                break
            candidate = [unit, *overlap]
            candidate_text = "\n".join(part.text for part in candidate)
            if meter.estimate_tokens(candidate_text) > overlap_budget:
                break
            if meter.estimate_tokens(f"{candidate_text}\n{next_unit.text}") > budget:
                break
            overlap = candidate
        return overlap

    def emit(emitted: _ChunkAccumulator) -> None:
        """输出一个块；只有重叠、没有新增内容的块不输出。"""
        if emitted.new_units <= 0 or not emitted.units:
            return
        drafts.append(
            ChunkDraft(
                content=emitted.content(),
                content_type=emitted.content_type(),
                section_path="",
                needs_manual_review=emitted.needs_manual_review(),
                review_reason=emitted.review_reason(),
            )
        )

    def flush(next_unit: _SentenceUnit | None = None) -> None:
        """输出当前块；有正文后继时，仅搬运预算允许的尾部句子。"""
        nonlocal accumulator
        if not accumulator.units:
            return
        emitted = accumulator
        carried = overlap_units(emitted, next_unit) if next_unit is not None else []
        accumulator = _ChunkAccumulator()
        emit(emitted)
        for unit in carried:
            accumulator.add(unit)

    for unit in units:
        if unit.content_type == "table" or unit.needs_manual_review:
            # 表格自带表头；待审核单位必须独立存放，均不参与正文重叠。
            flush()
            accumulator.add(unit)
            accumulator.new_units = 1
            emit(accumulator)
            accumulator = _ChunkAccumulator()
            continue
        if accumulator.units and meter.estimate_tokens(accumulator.project_content(unit)) > budget:
            flush(unit)
        if meter.estimate_tokens(accumulator.project_content(unit)) > budget:
            # 搬来的重叠占用了空间：优先保留本块的新句子。
            accumulator = _ChunkAccumulator()
        accumulator.add(unit)
        accumulator.new_units += 1
    flush()
    return drafts


def _iter_section_text(section: SectionData) -> str:
    """拼接章节直属内容，用于判断是否需要下钻。"""
    return "\n".join(block.text for block in section.blocks)


def _has_oversized_sentence(section: SectionData, meter: LengthMeter) -> bool:
    """判断章节正文里是否存在超出单句上限或单块预算的句子。

    章节整体不超预算时也必须检查：单个异常长句可能自己就超过单句上限，但整段仍在块预算
    之内；这类内容不能静默地整块送去向量化，需要交给句子打包分支标记待人工处理。
    """
    for block in section.blocks:
        if block.type != "text":
            continue
        if longest_sentence_tokens(split_sentences(block.text), meter) > min(
            rag_settings.effective_max_sentence_tokens, rag_settings.chunk_budget_tokens
        ):
            return True
    return False


def _annotate(drafts: list[ChunkDraft], section: SectionData) -> list[ChunkDraft]:
    """给切出的块标上它所属章节的元数据。

    questions 取“直接上级标题 + 本节标题”：这些块没有天然问法，标题就是用户会用的说法。
    带上直接上级标题是必要的限定语境（“七天无理由退货/适用条件”），只取文档标题会让同一份
    文档里所有块的向量化文本趋同，检索直接退化。
    """
    if len(section.path) >= 2:
        questions = (section.path[-2], section.path[-1])
    elif section.path:
        questions = (section.path[-1],)
    else:
        questions = ()
    key_clause = any(keyword in part for part in section.path for keyword in _KEY_CLAUSE_KEYWORDS)
    section_path = " > ".join(section.path)
    for draft in drafts:
        draft.section_path = section_path
        draft.questions = questions
        draft.is_key_clause = key_clause
    return drafts


def chunk_section(
    section: SectionData,
    meter: LengthMeter | None = None,
    depth: int = 0,
) -> list[ChunkDraft]:
    """把一个章节切分成知识块草稿。

    先处理章节直属内容：深层章节未超预算时可整体保留，超预算时按完整句子装箱。
    子章节始终按原顺序继续处理，即使到达标题下钻层数上限也不能丢弃其内容。
    """
    active_meter = meter or get_length_meter()
    section_text = _iter_section_text(section)
    section_tokens = active_meter.estimate_tokens(section_text)
    has_children = bool(section.children)
    can_descend = has_children and depth < rag_settings.max_heading_depth

    if not can_descend and section_tokens <= rag_settings.chunk_budget_tokens:
        if not section_text.strip():
            # 纯标题章节没有直属内容，只为子标题提供归属信息。
            drafts: list[ChunkDraft] = []
        elif not _has_oversized_sentence(section, active_meter) and not _section_has_special_block(section):
            drafts = _annotate(
                [ChunkDraft(content=section_text, content_type="text", section_path="")],
                section,
            )
        else:
            # 含异常长句或表格/代码块：标记超长内容并保持表格、代码块完整。
            drafts = _annotate(_pack_units(_content_units(section, active_meter), active_meter), section)
    else:
        drafts = _annotate(_pack_units(_content_units(section, active_meter), active_meter), section)

    for child in section.children:
        drafts.extend(chunk_section(child, active_meter, depth + 1))
    return drafts
