"""对话文本的个人与交易标识脱敏。

调用抽取模型**之前**就先处理手机号、地址、订单号、运单号、邮箱与证件号：这些信息一旦进入
模型输出、暂存表或正式知识，就等于把用户隐私写进了可被检索的知识库，事后清理的代价远高于
提前擦除。因此这里的处理是纯文本的确定性替换，不依赖模型自觉。

替换使用固定占位符（`[手机号]`、`[订单号]` 等），不用随机串：这样同一段对话每次都得到同一份
文本，批次签名与候选指纹才是稳定的，重复运行不会因为脱敏结果抖动而产生新知识。

`contains_redacted_marker` 的用途是判定候选能否入库：只描述某个用户订单状态、物流轨迹或
个人承诺的候选，脱敏后会带着占位符，例如“您的包裹已到 [地址]”。这类候选一律留在暂存表，
不进入正式知识——把占位符当成通用政策写进知识库会让检索返回残缺内容。
"""

from __future__ import annotations

import re

# 占位符统一用中文方括号包裹；正式知识不得出现这些标记。
PHONE_TOKEN = "[手机号]"
EMAIL_TOKEN = "[邮箱]"
ID_CARD_TOKEN = "[证件号]"
ORDER_TOKEN = "[订单号]"
TRACKING_TOKEN = "[运单号]"
ADDRESS_TOKEN = "[地址]"

_REDACTION_TOKENS = (
    PHONE_TOKEN,
    EMAIL_TOKEN,
    ID_CARD_TOKEN,
    ORDER_TOKEN,
    TRACKING_TOKEN,
    ADDRESS_TOKEN,
)

# 手机号：可选 +86 / 0086 前缀，允许空格、连字符与括号分隔（如 138-0013-8000、138 0013 8000）。
# 前后不允许紧邻数字或字母，避免把订单号、运单号中的长数字串误判成手机号。
_PHONE = re.compile(
    r"(?<![0-9A-Za-z])(?:\+?86[-\s]?|0086[-\s]?)?1[3-9][0-9](?:[-\s]?[0-9]{4}){2}(?![0-9A-Za-z])"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 身份证：18 位（末位可为 X）与 15 位两代格式。
_ID_CARD = re.compile(r"(?<![0-9A-Za-z])[1-9][0-9]{5}(?:19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx](?![0-9A-Za-z])")

# 带上下文标识的交易编号：先匹配关键词再吃掉冒号/空格后的编号本体。
# 编号本体允许字母、数字与连字符，并要求至少 6 位，避免把“订单状态”的普通名词当编号。
# 不匹配无上下文的裸编号串：纯数字形态无法与商品编号、金额、时间戳区分，误伤面太大；
# 真实对话里订单号/运单号几乎总带着“订单号”“运单号”这类标签，或前面提到的 # 号写法。
_ORDER_LABELLED = re.compile(
    r"(?:订单号|订单编号|订单流水号|订单ID|订单\s*id)\s*[:：#为是]?\s*[A-Za-z0-9][A-Za-z0-9\-]{5,}"
)
_TRACKING_LABELLED = re.compile(
    r"(?:运单号|快递单号|物流单号|包裹号|快递编号)\s*[:：#为是]?\s*[A-Za-z0-9][A-Za-z0-9\-]{5,}"
)

# 地址：省市自治区 + 可选地级市 + 可选区县，或“XX市XX区”，再或含“路/街/号/小区”的完整串。
# 只把行政区划与门牌号视为地址，不会命中“退款到账”这类普通短语。
# 门牌号前允许空格：真实地址常写成“文三路 100 号”。
_ADDRESS = re.compile(
    r"(?:[\u4e00-\u9fa5]{2,8}(?:省|自治区|特别行政区))?"
    r"[\u4e00-\u9fa5]{2,8}(?:市|自治州|地区|盟)"
    r"(?:[\u4e00-\u9fa5]{2,8}(?:区|县|旗|市))?"
    r"(?:[\u4e00-\u9fa5A-Za-z0-9]{1,20}(?:路|街|街道|巷|弄|大道))?"
    r"(?:\s*[0-9]{1,5}\s*号)?"
    r"(?:[\u4e00-\u9fa5A-Za-z0-9]{0,20}(?:小区|大厦|广场|公寓|花园|写字楼|号楼|栋|单元|室))?"
)
# 单独出现的短地址，例如“杭州市西湖区文三路”“北京市朝阳区”。
_ADDRESS_SHORT = re.compile(
    r"[\u4e00-\u9fa5]{2,6}(?:市|省)[\u4e00-\u9fa5]{2,8}(?:区|县|镇|街道|路|街)"
)


def _redact_address(text: str) -> str:
    """替换地址串；要求匹配结果至少包含行政区划或门牌号，避免命中普通词语。"""

    def replace(match: re.Match[str]) -> str:
        value = match.group(0)
        # 纯“XX市”这种两字加市名的短语可能是商品或政策名称的一部分，只有带区县、路街或门牌
        # 才认定为地址；单独一个市名不足以定位到个人。
        if not re.search(r"(区|县|镇|街道|路|街|巷|弄|大道|号|小区|大厦|广场|公寓|花园|号楼|栋|单元|室)", value):
            return value
        return ADDRESS_TOKEN

    redacted = _ADDRESS.sub(replace, text)
    return _ADDRESS_SHORT.sub(ADDRESS_TOKEN, redacted)


def redact_sensitive_text(text: str) -> str:
    """把手机号、邮箱、证件号、订单号、运单号与地址替换成固定占位符。

    顺序有讲究：先处理身份证与编号形态，再处理地址，最后处理手机号。手机号放在地址之后是
    因为地址规则可能吞掉带数字的门牌号，而手机号的边界断言不允许紧邻字母数字，先处理反而会
    留下部分数字。
    """
    if not text:
        return ""
    redacted = _ID_CARD.sub(ID_CARD_TOKEN, text)
    redacted = _EMAIL.sub(EMAIL_TOKEN, redacted)
    redacted = _ORDER_LABELLED.sub(ORDER_TOKEN, redacted)
    redacted = _TRACKING_LABELLED.sub(TRACKING_TOKEN, redacted)
    redacted = _PHONE.sub(PHONE_TOKEN, redacted)
    redacted = _redact_address(redacted)
    return redacted


def contains_redacted_marker(text: str) -> bool:
    """判断文本是否含脱敏占位符。

    含占位符说明这段内容依赖某个用户的个人信息或订单状态才能成立，属于“不可复用”的候选：
    它要么是订单状态播报，要么是物流轨迹，要么是个人承诺，都不能作为通用知识入库。
    """
    if not text:
        return False
    return any(token in text for token in _REDACTION_TOKENS)


def detect_sensitive_text(text: str) -> list[str]:
    """列出原文命中的敏感信息类别，用于抽取日志与候选拒绝原因。

    返回值是类别名（手机号、邮箱、证件号、订单号、运单号、地址），顺序固定便于比较。
    只报告“命中了哪类”，不回显命中的原始值，避免敏感信息进入日志。
    """
    if not text:
        return []
    found: list[str] = []
    if _ID_CARD.search(text):
        found.append("证件号")
    if _EMAIL.search(text):
        found.append("邮箱")
    if _ORDER_LABELLED.search(text):
        found.append("订单号")
    if _TRACKING_LABELLED.search(text):
        found.append("运单号")
    if _PHONE.search(text):
        found.append("手机号")
    if _redact_address(text) != text:
        found.append("地址")
    return found
