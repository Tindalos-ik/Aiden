import json
from typing import Any


def sse_event(name: str, payload: dict[str, Any]) -> str:
    """编码单条 SSE 事件，保持 remote.ts 所需的 event/data 双行格式。

    JSON 紧凑编码可安全承载换行和中文；末尾空行是 SSE 的事件边界，不能省略。
    """
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {data}\n\n"
