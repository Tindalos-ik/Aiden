"""切分用的长度计量与句子边界。

长度统一以 BGE-M3 的 token 计量，有两种模式：

* **exact**：`RAG_TOKENIZER_PATH` 指向本地 `tokenizer.json` 且安装了 `tokenizers` 时，
  按真实子词计数。这是推荐用法，长度判断与模型侧一致。
* **estimate**：缺省回退，按“字符数 / 每 token 字符数”估算。中文一个汉字常常对应多个
  子词，因此估算会偏乐观，配置里预留的 `measurement_uncertainty_tokens` 和
  `safety_margin_tokens` 就是为此扣减的预算，避免真实 token 数越过模型上限。

切分必须保证不输出半截句子，因此句子边界在此集中定义：段内先按换行断开，再按中英文
句末标点切开，并保留标点。这里的实现只做边界识别，不改写句子内容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from app.config.rag import rag_settings

MeasurementMode = Literal["exact", "estimate"]

# 段落内的硬换行：Markdown 里同一段很少跨行，硬换行通常意味着语义断点。
_LINE_BREAK = re.compile(r"\n+")
# 连续句末标点和省略号作为一个边界；西文句点只在后接空白与大写/汉字时切开，
# 避免把小数点及常见缩写中的句点当成句末。
_SENTENCE_END = re.compile(
    r"(?:[。！？；!?]|…{2,}|\.{3,})+|(?<=[A-Za-z0-9\)\]\"'%])\.(?=[\"'”’）\]】》」』]*\s+[A-Z\u4e00-\u9fff])"
)
_SENTENCE_CLOSERS = frozenset("\"'”’）]】》」』")
_TRAILING_WHITESPACE = re.compile(r"^[ \t\u3000]+|[ \t\u3000]+$")


@dataclass(frozen=True)
class LengthMeasurement:
    """当前的长度计量方式，随估算比例一起暴露给导入脚本做结果说明。"""

    tokenizer_path: str
    mode: MeasurementMode
    chars_per_token: float

    def describe(self) -> str:
        """给命令行输出一行可核对说明，方便判断切分预算是否可信。"""
        if self.mode == "exact":
            return f"长度计量：tokenizers 精确计数（{self.tokenizer_path}）"
        return (
            "长度计量：字符估算 "
            f"（{self.chars_per_token} 字符/token，未配置 RAG_TOKENIZER_PATH 或缺少 tokenizers）"
        )


class LengthMeter:
    """按配置对文本做 token 计量。

    exact 模式下的 tokenizer 是延迟加载的：模块导入不会读文件，只有真正测量时才加载，
    并且按路径缓存，避免在同一进程内重复初始化。
    """

    def __init__(self, tokenizer_path: str, chars_per_token: float, require_exact: bool) -> None:
        self._tokenizer_path = tokenizer_path
        self._chars_per_token = chars_per_token
        self._tokenizer = self._load_tokenizer(tokenizer_path) if tokenizer_path else None
        if self._tokenizer is None and require_exact:
            raise RuntimeError(
                "RAG_REQUIRE_EXACT_TOKENIZER 已开启，但无法加载精确分词器："
                "请安装 tokenizers 并把 RAG_TOKENIZER_PATH 指向 BGE-M3 的 tokenizer.json。"
            )

    @staticmethod
    def _load_tokenizer(tokenizer_path: str):
        """尝试加载本地 tokenizer.json；失败时返回 None 以便退回字符估算。"""
        try:
            from tokenizers import Tokenizer
        except ImportError:
            return None
        try:
            return Tokenizer.from_file(tokenizer_path)
        except Exception:
            # 路径写错或文件不是合法的 tokenizer.json 时退回估算模式，由命令行提示计量方式。
            return None

    @property
    def measurement(self) -> LengthMeasurement:
        """返回当前生效的计量方式。"""
        if self._tokenizer is not None:
            return LengthMeasurement(
                tokenizer_path=self._tokenizer_path, mode="exact", chars_per_token=self._chars_per_token
            )
        return LengthMeasurement(
            tokenizer_path="", mode="estimate", chars_per_token=self._chars_per_token
        )

    def estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数；空文本为 0。"""
        if not text:
            return 0
        if self._tokenizer is not None:
            # add_special_tokens=False：这里只量正文长度，特殊标记不计入切分预算。
            return len(self._tokenizer.encode(text, add_special_tokens=False).ids)
        return max(1, int(len(text) / self._chars_per_token + 0.999))

    def tokens_for_length(self, length: int) -> int:
        """仅在估算模式下按字符长度换算；精确模式必须计量实际文本。"""
        if self._tokenizer is not None:
            raise ValueError("精确分词模式不能从字符长度推算 token 数，请传入实际文本。")
        if length <= 0:
            return 0
        return max(1, int(length / self._chars_per_token + 0.999))


@lru_cache(maxsize=1)
def get_length_meter() -> LengthMeter:
    """按当前配置返回进程内唯一的长度计量器。"""
    return LengthMeter(
        tokenizer_path=rag_settings.embedding_tokenizer_path,
        chars_per_token=rag_settings.estimator_chars_per_token,
        require_exact=rag_settings.require_exact_tokenizer,
    )


def split_sentences(text: str) -> list[str]:
    """把一段正文切成完整句子，保留标点且不产生半截句子。

    先按硬换行分段，再按句末标点切分；连续标点和闭合引号留在前一句。未带句末标点的行尾
    视为段落边界，绝不按字符数截断正文。
    """
    sentences: list[str] = []
    for line in _LINE_BREAK.split(text):
        stripped_line = _TRAILING_WHITESPACE.sub("", line)
        if not stripped_line:
            continue
        cursor = 0
        for match in _SENTENCE_END.finditer(stripped_line):
            if match.start() < cursor:
                continue
            end = match.end()
            while end < len(stripped_line) and stripped_line[end] in _SENTENCE_CLOSERS:
                end += 1
            sentence = _TRAILING_WHITESPACE.sub("", stripped_line[cursor:end])
            if sentence:
                sentences.append(sentence)
            cursor = end
        remainder = _TRAILING_WHITESPACE.sub("", stripped_line[cursor:])
        if remainder:
            sentences.append(remainder)
    return sentences


def longest_sentence_tokens(sentences: list[str], meter: LengthMeter | None = None) -> int:
    """返回一组句子中最长的 token 数，用于判断句子是否超过单句上限。"""
    active_meter = meter or get_length_meter()
    return max((active_meter.estimate_tokens(sentence) for sentence in sentences), default=0)
