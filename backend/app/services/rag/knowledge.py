"""Markdown 知识文档解析与知识块构建。

职责分两层：

* `parse_markdown_document` 只做语法解析，把文档还原成保留章节归属的章节树；
* `build_document_chunks` / `build_faq_chunks` 把章节和 FAQ 行变成待入库的知识块，并补齐
  category、questions；文档块的 section_path、content_type 和关键条款标记由切分层保留。

商品 FAQ 的 questions 使用真实问题；政策或手册块没有天然问题，questions 使用所属章节
标题，category 使用该章节完整的上级标题路径。

解析时按标题出现顺序维护独立节点，最终转成不可变的章节树；同名标题不会覆盖先前章节。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from hashlib import sha256

from app.config.rag import rag_settings

from .chunking import Block, ChunkDraft, SectionData, chunk_section
from .length import LengthMeter, get_length_meter, longest_sentence_tokens, split_sentences

# 没有上级标题时给的分类兜底值。
DEFAULT_CATEGORY = "未分类"

# ATX 标题：允许最多三个前导空格，标题文本去掉结尾的井号。
_ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# 表格分隔行，例如 | --- | :---: |
_TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
# 章节标题里的 Markdown 链接：只保留可读文本，避免 section_path 混入 URL。
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# 关键条款判定用的标题关键词：退货、售后这类条款参与检索排序加权。
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


@dataclass(frozen=True)
class ParsedDocument:
    """解析后的 Markdown 文档；`fallback_title` 来自一级标题或调用方提供的文件名。"""

    fallback_title: str
    sections: tuple[SectionData, ...]


@dataclass
class _SectionNode:
    """解析中的章节节点；用对象身份区分同路径的多次标题出现。"""

    title: str
    level: int
    path: tuple[str, ...]
    blocks: list[Block] = field(default_factory=list)
    children: list[_SectionNode] = field(default_factory=list)

    def freeze(self) -> SectionData:
        """按文档顺序生成供切分层使用的不可变章节树。"""
        return SectionData(
            title=self.title,
            level=self.level,
            path=self.path,
            blocks=tuple(self.blocks),
            children=tuple(child.freeze() for child in self.children),
        )


class _SectionTree:
    """按标题出现顺序保存节点，避免同名标题按文字路径互相覆盖。"""

    def __init__(self) -> None:
        self.roots: list[_SectionNode] = []

    def add(self, parent: _SectionNode | None, title: str, level: int) -> _SectionNode:
        """在指定父节点下新增章节，即使标题路径与已有章节相同。"""
        path = (*parent.path, title) if parent is not None else (title,)
        node = _SectionNode(title=title, level=level, path=path)
        if parent is not None:
            parent.children.append(node)
        else:
            self.roots.append(node)
        return node

    def sections(self) -> tuple[SectionData, ...]:
        """冻结所有根节点及其子节点，保留重复标题和原有顺序。"""
        return tuple(root.freeze() for root in self.roots)


def _clean_heading_text(raw: str) -> str:
    """去掉标题中的链接语法和行内标记，只保留可读标题文本。"""
    text = _MARKDOWN_LINK.sub(r"\1", raw)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return text.strip()


def _is_table_start(lines: list[str], index: int) -> bool:
    """判断 index 行是否是表格起始行：本行含竖线且下一行是分隔行。"""
    if "|" not in lines[index]:
        return False
    if index + 1 >= len(lines):
        return False
    return bool(_TABLE_DIVIDER.match(lines[index + 1]))


def parse_markdown_document(text: str, fallback_title: str) -> ParsedDocument:
    """把 Markdown 文本解析成章节树。

    围栏代码块内部的 `#` 和 `|` 不会被当作标题或表格，因此文档中的代码示例不会破坏结构。
    文档开头、第一个标题之前的内容作为前言跳过，不生成知识块。
    """
    lines = text.splitlines()
    tree = _SectionTree()
    heading_stack: list[_SectionNode] = []
    first_heading: str | None = None
    buffer: list[str] = []
    buffer_type: str = "text"
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        """把缓冲区内容作为直属块挂到当前章节；前言内容直接丢弃。"""
        nonlocal buffer, buffer_type
        raw = "\n".join(buffer).strip("\n").strip()
        block_type = buffer_type
        buffer = []
        buffer_type = "text"
        if raw and heading_stack:
            heading_stack[-1].blocks.append(Block(type=block_type, text=raw))

    index = 0
    while index < len(lines):
        line = lines[index]
        fence_match = _FENCE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            if not in_fence:
                flush()
                in_fence = True
                fence_marker = marker
                buffer_type = "code"
                buffer.append(line)
            elif marker == fence_marker:
                buffer.append(line)
                flush()
                in_fence = False
                fence_marker = ""
            else:
                buffer.append(line)
            index += 1
            continue

        if in_fence:
            buffer.append(line)
            index += 1
            continue

        heading_match = _ATX_HEADING.match(line)
        if heading_match:
            flush()
            level = len(heading_match.group(1))
            title = _clean_heading_text(heading_match.group(2))
            if first_heading is None and level == 1:
                first_heading = title
            # 标题层级决定父节点，节点身份决定内容归属；同名兄弟章节仍各自独立。
            while heading_stack and heading_stack[-1].level >= level:
                heading_stack.pop()
            parent = heading_stack[-1] if heading_stack else None
            heading_stack.append(tree.add(parent, title, level))
            index += 1
            continue

        if _is_table_start(lines, index):
            flush()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            if heading_stack:
                heading_stack[-1].blocks.append(Block(type="table", text="\n".join(table_lines)))
            continue

        buffer.append(line)
        index += 1

    flush()
    return ParsedDocument(
        fallback_title=first_heading or fallback_title,
        sections=tree.sections(),
    )


def category_for(section: SectionData, document_title: str) -> str:
    """分类使用完整上级标题路径；根章节退回文档标题。"""
    if len(section.path) >= 2:
        return " > ".join(section.path[:-1])
    return document_title or DEFAULT_CATEGORY


def is_key_clause(section: SectionData) -> bool:
    """按章节路径中的标题关键词判断是否为退货、售后这类关键条款。"""
    return any(keyword in part for part in section.path for keyword in _KEY_CLAUSE_KEYWORDS)


def build_document_chunks(
    parsed: ParsedDocument,
    source_title: str | None = None,
    meter: LengthMeter | None = None,
) -> list[ChunkDraft]:
    """把解析后的文档切成知识块，并补齐每块所属的分类。

    `chunk_section` 保留内容、顺序、章节路径及其他元数据；这里按每块的章节路径补齐完整
    上级标题分类，并让 questions 只使用所属章节标题。
    """
    active_meter = meter or get_length_meter()
    document_title = (source_title or parsed.fallback_title or "").strip()
    chunks: list[ChunkDraft] = []

    def section_metadata(section: SectionData) -> dict[str, tuple[str, str]]:
        """索引整棵子树，供递归切出的块按所属章节取分类和标题。"""
        metadata = {
            " > ".join(section.path): (category_for(section, document_title), section.title)
        }
        for child in section.children:
            metadata.update(section_metadata(child))
        return metadata

    for section in parsed.sections:
        metadata = section_metadata(section)
        for draft in chunk_section(section, active_meter):
            if not draft.content.strip():
                continue
            draft.category, title = metadata[draft.section_path]
            draft.questions = (title,) if title else ()
            chunks.append(draft)
    return chunks


def build_faq_chunks(
    rows: list[dict[str, str | None]],
    meter: LengthMeter | None = None,
) -> list[ChunkDraft]:
    """把启用中的 FAQ 行转成知识块。

    每条 FAQ 天然带真实问题，因此 questions 直接用它的 question 字段，category 用它自己的
    分类。FAQ 答案整条作为一块，不再按章节切分。
    """
    active_meter = meter or get_length_meter()
    chunks: list[ChunkDraft] = []
    for row in rows:
        answer = (row.get("answer") or "").strip()
        question = (row.get("question") or "").strip()
        if not answer:
            continue
        category = (row.get("category") or "").strip() or DEFAULT_CATEGORY
        needs_review = _faq_needs_review(answer, active_meter)
        chunks.append(
            ChunkDraft(
                content=answer,
                content_type="text",
                section_path=category,
                needs_manual_review=needs_review,
                review_reason="FAQ 答案超过单块预算，需人工拆分后再向量化" if needs_review else None,
                category=category,
                questions=(question,) if question else (),
                is_key_clause=any(keyword in category for keyword in _KEY_CLAUSE_KEYWORDS),
            )
        )
    return chunks


def _faq_needs_review(answer: str, meter: LengthMeter) -> bool:
    """FAQ 答案超过单块预算或含异常长句时标记待人工处理，不做静默截断。"""
    if meter.estimate_tokens(answer) > rag_settings.chunk_budget_tokens:
        return True
    return longest_sentence_tokens(split_sentences(answer), meter) > rag_settings.effective_max_sentence_tokens
def content_hash(text: str) -> str:
    """计算块内容的稳定哈希，用于识别内容变更并让旧向量作废。"""
    return sha256(text.encode("utf-8")).hexdigest()
