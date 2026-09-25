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
# 中日韩句末标点：这些标点后一定是句子边界，可以直接切开。
_CJK_SENTENCE_END = re.compile(r"(?<=[。！？；!?])")
# 西文句点只在小写/数字之后、空白与大写出现时才当作句末，避免切开 3.14、No. 1 等写法。
_LATIN_SENTENCE_END = re.compile(r"(?<=[a-z0-9\)\]\"'%])\.(?=\s+[A-Z\u4e00-\u9fff])")
# 省略号、连续标点等整体视为一个句末标记，避免被 _CJK_SENTENCE_END 拆成多段。
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
        """按字符长度换算 token 数，不构造实际文本。

        切分时频繁判断“再加这段会不会超预算”，用长度换算可以避免反复拼接整段正文。两种
        模式的换算口径都与 `estimate_tokens` 保持一致，否则预算判断会与实际计量口径不同。
        """
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

    处理顺序：先按硬换行分段，再在每段内按中文句末标点切分，最后处理西文句点。切分只
    在标点之后发生，因此每个返回值都是原文档中的完整连续子串；空白片段会被丢弃。
    """
    sentences: list[str] = []
    for line in _LINE_BREAK.split(text):
        stripped_line = _TRAILING_WHITESPACE.sub("", line)
        if not stripped_line:
            continue
        for chunk in _CJK_SENTENCE_END.split(stripped_line):
            if not chunk:
                continue
            # 西文句点再切一次：上一步已保证不会在 CJK 标点内部误切。
            for part in _split_on_latin_period(chunk):
                cleaned = _TRAILING_WHITESPACE.sub("", part)
                if cleaned:
                    sentences.append(cleaned)
    return sentences


def _split_on_latin_period(text: str) -> list[str]:
    """按西文句末句点切分，并让句点留在前一句末尾。"""
    parts: list[str] = []
    cursor = 0
    for match in _LATIN_SENTENCE_END.finditer(text):
        end = match.start() + 1  # 句点本身归入前一句
        parts.append(text[cursor:end])
        cursor = end
    parts.append(text[cursor:])
    return parts


def longest_sentence_tokens(sentences: list[str], meter: LengthMeter | None = None) -> int:
    """返回一组句子中最长的 token 数，用于判断句子是否超过单句上限。"""
    active_meter = meter or get_length_meter()
    return max((active_meter.estimate_tokens(sentence) for sentence in sentences), default=0)
