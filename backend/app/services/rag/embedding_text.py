"""向量化文本的唯一拼接格式。

送进 BGE-M3 dense embedding 的文本 **只** 由 category、questions、answer 三段拼成，格式由
本模块单点定义：离线建库与在线检索都必须走 `build_embedding_text`，避免两端拼出不一致的
文本——query 与库中向量的模板不一致，相似度就失去可比性。

明确不参与 embedding 的内容：section_path、content_type、is_key_clause、前后块指针。它们只
作为 Milvus 的标量字段和 MySQL 的元数据保存，用于过滤与排序加权。

`build_embedding_text` 对同样的输入必须始终返回同样的字符串，因此这里不使用集合、字典等
无序结构，也不做随机化处理；`embedding_fingerprint` 记录模板与格式版本，用来判断“文本模板
换过”还是“正文改过”。
"""

from __future__ import annotations

from hashlib import sha256

# 模板版本：格式一旦调整必须递增，便于识别历史向量是按哪版格式生成的。
EMBEDDING_TEXT_TEMPLATE_VERSION = "v1"

# 同一块多个真实问法之间的分隔符：使用全角竖线，避免与中文问句里的标点冲突。
_QUESTIONS_SEPARATOR = "｜"

# 固定顺序的字段标签，保证拼接结果稳定且可读、便于人工核对。
_CATEGORY_LABEL = "分类"
_QUESTIONS_LABEL = "问题"
_ANSWER_LABEL = "答案"

# 在线 query 侧的固定分类值。它不是业务分类，只用于让 query 文本与知识块文本在同一个模板下
# 对齐；知识块的 category 是权威业务分类，不能被 query 冒用。
QUERY_CATEGORY_LABEL = "用户提问"


def build_embedding_text(category: str, questions: list[str] | tuple[str, ...], answer: str) -> str:
    """按唯一格式拼接向量化文本。

    输入：category（分类）、questions（真实问法列表，可为空）、answer（知识正文）。
    输出：固定三段、固定顺序、固定标签的 UTF-8 文本；空白问法会被丢弃，全部为空时该行
    整行省略，避免出现“问题：”这样的空标签影响向量语义。
    """
    clean_questions = [question.strip() for question in questions if question and question.strip()]
    lines = [f"{_CATEGORY_LABEL}：{category.strip()}"]
    if clean_questions:
        lines.append(f"{_QUESTIONS_LABEL}：{_QUESTIONS_SEPARATOR.join(clean_questions)}")
    lines.append(f"{_ANSWER_LABEL}：{answer.strip()}")
    return "\n".join(lines)


def build_query_embedding_text(question: str) -> str:
    """把用户在线提问拼成与知识块同构的向量化文本。

    在线检索必须复用 `build_embedding_text`，让 query 向量与库中向量出自同一份模板：模板里
    的固定字段标签本身参与向量语义，如果 query 直接送裸问句，它就与库中带标签的文本落在
    向量空间的不同位置，相似度不再可比。

    分类固定用 `QUERY_CATEGORY_LABEL`、答案留空：query 侧没有权威分类和正文，用知识块的真实
    category 去凑会凭空引入“这是什么分类”的猜测；而“问题”段是知识块与 query 唯一共有的语义
    锚点，因此只保留它。
    """
    return build_embedding_text(QUERY_CATEGORY_LABEL, [question], "")


def embedding_fingerprint() -> str:
    """返回当前拼接格式的指纹。

    指纹只取决于模板版本、字段顺序与分隔符定义，不随知识内容变化：内容变更由每块的
    content_hash 记录，两者结合即可区分“模板变了”和“正文变了”。
    """
    layout = "\n".join(
        [
            EMBEDDING_TEXT_TEMPLATE_VERSION,
            _CATEGORY_LABEL,
            _QUESTIONS_LABEL,
            _QUESTIONS_SEPARATOR,
            _ANSWER_LABEL,
        ]
    )
    return sha256(layout.encode("utf-8")).hexdigest()
