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
_MANUAL_REVIEW_TABLE_ROW = "表格单行宽度超过单块预算，需人工拆分后再向量化"

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


def _tokens_of_length(length: int, meter: LengthMeter) -> int:
    """按拼接后长度换算 token 数。

    切换预算判断到“按长度换算”后，必须和 `estimate_tokens` 用同一套口径，否则估算模式会
    低估（中文一个汉字常对应多个子词，逐单位相加会漏掉子词边界的开销）。
    """
    return meter.tokens_for_length(length)


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
    """打包用的最小单位：一段完整段落/句子、一张已分块的表格或一个代码块。"""

    text: str
    content_type: Literal["text", "table", "code"]
    needs_manual_review: bool = False
    review_reason: str | None = None
    # 表格分块内部按行平铺，记录行数便于块级长度校验。
    line_count: int = 0
    # 表格分块之间必须断开：每个表格块都带了自己的表头，合并回去会让“每块复制表头、
    # 独立理解”的设计失效，也让 RAG_TABLE_ROWS_PER_CHUNK 失去作用。
    break_after: bool = False


@dataclass
class _ChunkAccumulator:
    """打包过程中的可变状态；仅内部使用。

    单位之间最终会用换行拼成一块，而长度计量的结果依赖整段文本（子词边界会随拼接变化），
    因此这里不累加各单位自己的 token 数，而是用 `assembled_length` 记录拼接后的长度，再统一
    换算 token：这样预算判断针对的正是最终要送去向量化的那段文本。

    `new_units` 记录当前块里“本块新增”的单位数（不含从上一块搬来的重叠前缀）：只有重叠、
    没有新增内容的块不应该被输出，否则会在输入本身重复时产出内容完全相同的块。
    """

    units: list[_SentenceUnit] = field(default_factory=list)
    assembled_length: int = 0
    new_units: int = 0

    def content(self) -> str:
        return "\n".join(unit.text for unit in self.units)

    def project_length(self, unit: _SentenceUnit) -> int:
        """把某个单位并入本块后，拼接文本会变成多长。"""
        if not self.units:
            return len(unit.text)
        return self.assembled_length + 1 + len(unit.text)

    def add(self, unit: _SentenceUnit) -> None:
        """把单位并入本块，并同步拼接长度。"""
        self.assembled_length = self.project_length(unit)
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
    header_tokens = active_meter.estimate_tokens(header_text)
    data_rows = rows[2:]
    if not data_rows:
        return [_SentenceUnit(text=header_text, content_type="table")]

    budget = rag_settings.chunk_budget_tokens
    row_limit = max(1, rag_settings.table_rows_per_chunk)
    units: list[_SentenceUnit] = []
    current_rows: list[str] = []
    current_tokens = header_tokens

    def flush() -> None:
        """把当前累计的数据行连同表头输出为一块，并重置累计。

        每个表格块都标记 `break_after=True`，避免打包时把两个各带表头的表格块合并成一块。
        """
        nonlocal current_rows, current_tokens
        if not current_rows:
            return
        units.append(
            _SentenceUnit(
                text="\n".join([*header_rows, *current_rows]),
                content_type="table",
                line_count=len(current_rows),
                break_after=True,
            )
        )
        current_rows = []
        current_tokens = header_tokens

    for row in data_rows:
        row_tokens = active_meter.estimate_tokens(row)
        if header_tokens + row_tokens > budget:
            # 单行加表头已超预算：无法在保住完整行的前提下安全向量化。
            flush()
            units.append(
                _SentenceUnit(
                    text="\n".join([*header_rows, row]),
                    content_type="table",
                    needs_manual_review=True,
                    review_reason=_MANUAL_REVIEW_TABLE_ROW,
                    line_count=1,
                    break_after=True,
                )
            )
            continue
        if current_rows and (len(current_rows) >= row_limit or current_tokens + row_tokens > budget):
            flush()
        current_rows.append(row)
        current_tokens += row_tokens
    flush()
    return units


def _sentence_units(text: str, meter: LengthMeter) -> list[_SentenceUnit]:
    """把正文切成句子单位，并标记超过单句上限的异常长句。

    这里按换行分段，而不是把整段正文压平：段落边界是原文的真实分隔方式，也是用户阅读和
    模型理解的自然单位。只有单个段落自身超过单句上限时，才进一步下钻到句子粒度——那种
    情况通常意味着一段里塞了太多内容，按句子拆开比整段送更安全，也更容易人工核对。
    """
    units: list[_SentenceUnit] = []
    for line in text.splitlines():
        paragraph = line.strip()
        if not paragraph:
            continue
        paragraph_tokens = meter.estimate_tokens(paragraph)
        if paragraph_tokens <= rag_settings.effective_max_sentence_tokens:
            units.append(_SentenceUnit(text=paragraph, content_type="text"))
            continue
        for sentence in split_sentences(paragraph):
            sentence_tokens = meter.estimate_tokens(sentence)
            units.append(
                _SentenceUnit(
                    text=sentence,
                    content_type="text",
                    needs_manual_review=sentence_tokens > rag_settings.effective_max_sentence_tokens,
                    review_reason=_MANUAL_REVIEW_SENTENCE
                    if sentence_tokens > rag_settings.effective_max_sentence_tokens
                    else None,
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
            units.append(_SentenceUnit(text=block.text, content_type="code"))
        else:
            units.extend(_sentence_units(block.text, meter))
    return units


def _pack_units(units: list[_SentenceUnit], meter: LengthMeter) -> list[ChunkDraft]:
    """把单位序列按预算装箱成块，并在相邻块之间加入完整句子的重叠。

    重叠只从上一块尾部回退取完整单位，且总量不超过 `RAG_OVERLAP_TOKENS`：这样下一块开头
    带着上一块结尾的完整句子，跨块语义不会断在句子中间。回退的单位写入新块时不会被再次
    计入“上一块尾部”，因为每块只从它的前一块取一次重叠。

    装箱时的可用预算会先扣掉重叠预留量：新块开头要放上一块的尾部单位，如果按完整预算装
    满，加上重叠就会越过单块上限。
    """
    drafts: list[ChunkDraft] = []
    accumulator = _ChunkAccumulator()
    budget = rag_settings.chunk_budget_tokens
    overlap_budget = rag_settings.overlap_tokens
    # 预留重叠空间，保证“本块新增内容 + 上一块尾部”仍在单块预算内。
    packing_budget = max(budget - overlap_budget, 1)

    def overlap_units(source: _ChunkAccumulator) -> list[_SentenceUnit]:
        """从块尾回退取不超过重叠预算的完整单位。

        重叠必须由完整单位组成，因此从尾部一个一个加，并用拼接长度换算 token，而不是累加
        各单位自己的 token（子词边界会随拼接变化，累加会低估）。
        """
        overlap: list[_SentenceUnit] = []
        overlap_length = 0
        for unit in reversed(source.units):
            extended = len(unit.text) if not overlap else overlap_length + 1 + len(unit.text)
            if overlap and _tokens_of_length(extended, meter) > overlap_budget:
                break
            overlap.append(unit)
            overlap_length = extended
        overlap.reverse()
        return overlap

    def apply_overlap(
        units_to_apply: list[_SentenceUnit], emitted: _ChunkAccumulator
    ) -> None:
        """把上一块的重叠前缀放进当前累计器，并如实标记新增内容。

        当前块已有的单位视为“本块新增内容”（含被吸进来的重叠单位）；单独搬过来的重叠前缀
        会把 new_units 归零，这样子块变少时不会因为“只有重叠”而产出一个完全重复的块。
        """
        if not units_to_apply:
            return
        accumulator.units = list(units_to_apply)
        accumulator.assembled_length = len("\n".join(unit.text for unit in units_to_apply))
        if units_to_apply and units_to_apply[0] is emitted.units[0]:
            accumulator.new_units = len(units_to_apply)
        else:
            accumulator.new_units = 0

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

    def flush(*, keep_overlap: bool = True) -> None:
        """输出当前累计块，并按需保留尾部重叠供下一块使用。

        `keep_overlap=False` 用于表格这类自带表头、语义自洽的块：它们不需要跨块重叠，而且
        保留重叠会让下一块把上一块的表体再抄一遍，造成数据行重复。
        """
        nonlocal accumulator
        if not accumulator.units:
            return
        emitted = accumulator
        carried = overlap_units(emitted) if keep_overlap else []
        accumulator = _ChunkAccumulator()
        emit(emitted)
        apply_overlap(carried, emitted)

    for unit in units:
        if accumulator.units:
            if _tokens_of_length(accumulator.project_length(unit), meter) > packing_budget:
                if accumulator.new_units == 0:
                    # 本块只有搬来的重叠前缀，放不下新单位时收回前缀，让单位自己开新块，
                    # 避免先产出一个只含重叠的重复块。
                    accumulator = _ChunkAccumulator()
                else:
                    flush()
        if unit.needs_manual_review and unit.content_type == "text":
            # 异常长句必须整句独立成块：先冲刷已有内容，再把这一句单独输出，之后重置累计器，
            # 既避免它和邻居合并，也避免把这句话重复输出两次。
            flush()
            accumulator = _ChunkAccumulator()
            accumulator.add(unit)
            accumulator.new_units = 1
            emit(accumulator)
            accumulator = _ChunkAccumulator()
            continue
        accumulator.add(unit)
        accumulator.new_units += 1
        if unit.break_after:
            # 表格块自带表头，必须与下一块硬断开，保证每块独立理解且数据行不重复。
            flush(keep_overlap=False)
    flush()
    return drafts


def _iter_section_text(section: SectionData) -> str:
    """拼接章节直属内容，用于判断是否需要下钻。"""
    return "\n".join(block.text for block in section.blocks)


def _has_oversized_sentence(section: SectionData, meter: LengthMeter) -> bool:
    """判断章节正文里是否存在超过单句上限的句子。

    章节整体不超预算时也必须检查：单个异常长句可能自己就超过单句上限，但整段仍在块预算
    之内；这类内容不能静默地整块送去向量化，需要交给句子打包分支标记待人工处理。
    """
    for block in section.blocks:
        if block.type != "text":
            continue
        if longest_sentence_tokens(split_sentences(block.text), meter) > rag_settings.effective_max_sentence_tokens:
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

    先计算章节直属内容的 token 数：不超预算就直接成块（保持章节整体性）；超预算时若还有
    更深的子标题就递归下钻，否则按句子打包。子标题用尽或到达层数上限时同样落到句子打包，
    因此递归一定收敛。
    """
    active_meter = meter or get_length_meter()
    section_text = _iter_section_text(section)
    section_tokens = active_meter.estimate_tokens(section_text)
    has_children = bool(section.children)
    can_descend = has_children and depth < rag_settings.max_heading_depth

    if not can_descend and section_tokens <= rag_settings.chunk_budget_tokens:
        if not section_text.strip():
            # 纯标题章节没有直属内容，只为子标题提供归属信息。
            return []
        if not _has_oversized_sentence(section, active_meter) and not _section_has_special_block(section):
            return _annotate(
                [ChunkDraft(content=section_text, content_type="text", section_path="")],
                section,
            )
        # 含异常长句或表格/代码块：走单位打包分支，标记超长内容并保持表格、代码块完整。
        return _annotate(_pack_units(_content_units(section, active_meter), active_meter), section)

    if can_descend:
        # 子标题是更细的语义边界：先用句子打包消化本章节直属正文，再递归处理子章节。
        drafts = _annotate(_pack_units(_content_units(section, active_meter), active_meter), section)
        for child in section.children:
            drafts.extend(chunk_section(child, active_meter, depth + 1))
        return drafts

    return _annotate(_pack_units(_content_units(section, active_meter), active_meter), section)
