"""Markdown 知识文档解析与知识块构建。

职责分两层：

* `parse_markdown_document` 只做语法解析，把文档还原成保留章节归属的章节树；
* `build_document_chunks` / `build_faq_chunks` 把章节和 FAQ 行变成待入库的知识块，在这里
  决定 category、questions、section_path、content_type 和关键条款标记。

商品 FAQ 的 questions 使用真实问题；政策或手册块没有天然问题，questions 退回所在章节
标题（标题本身就是用户会问的说法），category 使用上级标题路径。

章节树用“路径 -> 章节”的字典维护，标题层级只需按路径前缀归位，不需要在解析过程中反复
重建父节点引用；章节对象本身是不可变数据类，追加内容时替换整棵路径上的对象。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
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


class _SectionTree:
    """解析过程中的可变章节树：用路径索引定位章节，再回写不可变节点。"""

    def __init__(self) -> None:
        self._nodes: dict[tuple[str, ...], SectionData] = {}
        self.roots: list[SectionData] = []

    def add(self, parent_path: tuple[str, ...], title: str, level: int) -> None:
        """在指定父路径下新增一个章节节点。"""
        path = (*parent_path, title)
        self._nodes[path] = SectionData(title=title, level=level, path=path)
        if parent_path:
            self._replace(parent_path, lambda node: replace(node, children=(*node.children, self._nodes[path])))
        else:
            self.roots.append(self._nodes[path])

    def append_block(self, path: tuple[str, ...], block: Block) -> None:
        """把内容块挂到指定章节上。"""
        self._replace(path, lambda node: replace(node, blocks=(*node.blocks, block)))

    def section_paths(self) -> list[tuple[str, ...]]:
        """按文档顺序返回已出现的章节路径。"""
        return list(self._nodes)

    def _replace(self, path: tuple[str, ...], transform) -> None:
        """就地更新一个节点，并把变更向上冒泡，保证祖先持有的子树包含该节点。"""
        node = self._nodes[path]
        updated = transform(node)
        self._nodes[path] = updated
        self._bubble(path, updated)

    def _bubble(self, path: tuple[str, ...], child: SectionData) -> None:
        """把子节点的更新同步到其所有祖先与根列表。"""
        parent_path = path[:-1]
        while parent_path:
            parent = self._nodes[parent_path]
            children = tuple(child if existing.path == child.path else existing for existing in parent.children)
            updated_parent = replace(parent, children=children)
            self._nodes[parent_path] = updated_parent
            child = updated_parent
            path = parent_path
            parent_path = path[:-1]
        self.roots = [
            child if existing.path == child.path else existing for existing in self.roots
        ]


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
    文档开头、第一个标题之前的内容挂在空路径节点上，只作为文档前言，不生成知识块。
    """
    lines = text.splitlines()
    tree = _SectionTree()
    current_path: tuple[str, ...] = ()
    first_heading: str | None = None
    buffer: list[str] = []
    buffer_type: str = "text"
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        """把缓冲区内容作为直属块挂到当前章节；前言内容直接丢弃。"""
        nonlocal buffer, buffer_type
        raw = "\n".join(buffer).strip("\n").strip()
        buffer = []
        buffer_type = "text"
        if raw and current_path:
            tree.append_block(current_path, Block(type=buffer_type, text=raw))

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
            # 同级或更浅的新标题会关闭所有更深层级；父路径就是长度小于本级的最近路径。
            parent_path = current_path
            while parent_path and len(parent_path) >= level:
                parent_path = parent_path[:-1]
            tree.add(parent_path, title, level)
            current_path = (*parent_path, title)
            index += 1
            continue

        if _is_table_start(lines, index):
            flush()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and lines[index].strip() and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            if current_path:
                tree.append_block(current_path, Block(type="table", text="\n".join(table_lines)))
            continue

        buffer.append(line)
        index += 1

    flush()
    return ParsedDocument(
        fallback_title=first_heading or fallback_title,
        sections=tuple(tree.roots),
    )


def category_for(section: SectionData, document_title: str) -> str:
    """按“上级标题路径”给出分类；没有上级标题时退回文档标题。

    只取直接上级标题（`path[-2]`）：完整路径会无限拉长分类，而检索过滤需要的是稳定的
    业务分类粒度，例如“七天无理由退货”“受理范围”。
    """
    if len(section.path) >= 2:
        return section.path[-2]
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

    questions / is_key_clause 由 `chunk_section` 按“产出该块的章节”填写：递归下钻时块来自更深
    的子章节，只有切分过程知道它的直接归属。这里只补 category，因为分类按顶层业务标题取值，
    对同一份文档内的块是稳定的。
    """
    active_meter = meter or get_length_meter()
    document_title = (source_title or parsed.fallback_title or "").strip()
    chunks: list[ChunkDraft] = []
    for section in parsed.sections:
        category = category_for(section, document_title)
        for draft in chunk_section(section, active_meter):
            if not draft.content.strip():
                continue
            draft.category = category
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
