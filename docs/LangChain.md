# LangChain：从消息与模型接口到 Aiden 的受控工具链

阅读入口：[技术学习路线](技术学习路线.md) → 本文 → [LangGraph](LangGraph.md) → [Agent 实现详解](Agent实现详解.md)。HTTP 与流式协议见 [FastAPI](FastAPI.md)，证据检索见 [RAG](RAG.md)。

## 1. 它解决什么问题

LLM 服务接受消息，返回消息；数据库查询接受业务参数，返回事实。LangChain 提供模型接口、消息类型、可调用组件和工具描述，把这些不同接口接起来。它不自动赋予模型业务权限，也不保证答案真实。

Aiden 的依赖声明为 `langchain>=1.0,<2.0`、`langchain-openai>=0.2.10,<2.0`，见 [requirements.txt](../backend/requirements.txt)。代码也直接使用 `langchain_core` 的接口；声明范围不等于当前安装版本。以下先讲机制，再标出项目实际使用的部分。

## 2. Messages：不要把所有内容拼成一段字符串

| 消息类型 | 含义 | 本项目用途 |
| --- | --- | --- |
| `SystemMessage` | 行为约束、当前任务指令 | 客服约束或结构化识别指令 |
| `HumanMessage` | 用户输入 | 当前原话、有限历史、单项问题 |
| `AIMessage` | 助手输出，也可承载工具调用 | 模型回答或服务端合成的调用记录 |
| `ToolMessage` | 某次工具调用的结果 | 本轮查询事实、召回证据 |

一个**只演示消息结构、不调用模型或数据库**的小例子：

```python
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

messages = [HumanMessage(content="查询订单")]
call = {"name": "lookup_demo", "args": {}, "id": "demo-1", "type": "tool_call"}
messages.append(AIMessage(content="", tool_calls=[call]))
messages.append(ToolMessage(content='{"status":"ok"}', tool_call_id="demo-1", name="lookup_demo"))
```

`tool_call_id` 把结果关联到调用；它不是用户身份。这个示例中的结果是教学常量，不是服务验收。消息结构能表达调用关系，但不能证明工具确实运行，也不能证明输出内容可靠。

项目的 [nodes/tools.py](../backend/app/agent/nodes/tools.py) 中 `make_dispatch_tool_call()` 在服务端构造 `AIMessage.tool_calls`，之后交给 `ToolNode` 执行。不要看到 `AIMessage` 就误认为调用是模型自主提出的。

## 3. Runnable：统一调用方式，不等于自动工作流

Runnable 可以理解为“接受输入并产生输出的组件”。常见接口：

- `invoke`：同步取得完整结果。
- `ainvoke`：异步取得完整结果。
- `stream` / `astream`：逐片段取得结果。
- `bind`：返回绑定调用参数的组件；不是立即发请求。
- `with_config`：绑定 callbacks、metadata 等执行配置；不是修改业务权限。

`ainvoke` 的异步指等待过程不阻塞当前协程；它仍然要等到一个完整输出才能继续。`astream` 则允许在结束前消费片段。流式与非流式不是两个不同业务答案来源，而是输出交付方式不同。

Aiden 在 [models.py](../backend/app/agent/models.py) 的 `create_models()` 创建两个客户端：

1. 答复模型 `ChatOpenAI`：`streaming=True`，温度 0.2、`max_tokens=800`。
2. 识别模型：另一个 `ChatOpenAI`，`streaming=False`，再 `.bind(response_format={"type": "json_object"})`。

创建客户端本身不发模型请求。请求发生在节点的 `ainvoke` 或 `astream`。`OPENAI_BASE_URL` 可用于兼容接口，但兼容服务对参数、思考字段和 usage 的支持需要实际核对，不是“能填地址就完全兼容”。项目还按配置控制 `stream_usage`、思考模式参数；不在文档写入实际密钥。

## 4. JSON 接口与结构校验是两层

“请输出 JSON”只约束输出形状，不保证字段符合业务枚举、范围或来源要求。Aiden 的链路是：

```text
Pydantic schema → 识别提示词中的 JSON Schema
→ json_model.ainvoke → 响应正文
→ SupportRequests.model_validate_json → 服务端路由复核
```

源码：[intent.py](../backend/app/agent/intent.py) 的 `SupportRequest` / `SupportRequests`、[nodes/intent.py](../backend/app/agent/nodes/intent.py) 的 `_structured_messages()` / `make_recognize_intent()`。

项目实际使用 JSON 模式加 Pydantic 校验，**没有使用 `with_structured_output()` 作为当前识别入口**。最多四个诉求；格式异常会再尝试一次；目标不明确或存在订单/退款上下文时才带近期历史再识别。多诉求还检查 `original_question_quote` 确实来自本轮原话。

教学例子（仅本地校验）：

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["order", "other"]
    confidence: float = Field(ge=0, le=1)

parsed = Intent.model_validate_json('{"intent":"order","confidence":0.8}')
```

这个例子只证明 JSON 能通过 schema，不证明订单存在或用户有权读取。项目保留实际契约拼写 `cofidence`，不能把教学字段拼写套到真实请求上。

## 5. 工具：描述、执行和授权要分开

`@tool` 根据函数名称、签名、类型标注和说明构造工具对象；它不是远程服务，也不自动调用函数。Aiden 在 [registry.py](../backend/app/services/tools/registry.py) 的 `SUPPORT_TOOLS` 静态登记八个工具：订单、物流、知识、售后、退款政策、退款登记、工单登记、工单查询。

最小教学例子只做内存计算：

```python
from langchain_core.tools import tool

@tool
def add_demo(left: int, right: int) -> int:
    """仅演示工具参数和返回值，不访问业务数据。"""
    return left + right

result = add_demo.invoke({"left": 2, "right": 3})
```

项目采用三层分工：

1. 模型识别受限意图与目标，不接收业务工具定义，不返回用户身份。
2. `route_intent()` 根据置信度、原话与已核验事实设置白名单；`make_dispatch_tool_call()` 决定工具名和参数。
3. 单工具 `ToolNode` 执行，`InjectedState` 注入路由提供的 `user_id` 等可信字段；仓储再在 SQL 中限制数据归属。

见 [nodes/intent.py](../backend/app/agent/nodes/intent.py)、[nodes/tools.py](../backend/app/agent/nodes/tools.py)、[customer.py](../backend/app/services/tools/customer.py) 的 `query_order()` / `query_logistics()`。工具注册、意图授权、SQL 隔离各自不能替代另外两层。退款登记还依赖跨轮确认和现行商家政策，详见 [售后与工单](售后与工单.md)。

**这里不是模型自主决定下一步工具的 ReAct。** 调用记录是服务端合成的，图的业务循环由服务端条件边控制；最终生成节点不承担工具选择职责。

## 6. 模型流不等于浏览器 SSE

[answer.py](../backend/app/agent/nodes/answer.py) 的 `make_generate()` 内部：

```text
model.astream → 提取可展示正文块
→ support_answer_delta 自定义事件
→ FastAPI 只转发 generate 节点内该事件为 delta
→ 引用核验 → save_answer 提交 → final → done
```

正文提取由 `_display_text()` 完成，推理块、工具调用块等不作为答复显示。`delta` 是尚未完成引用核验的草稿，核验失败时保存的最终回答可能是拒答。`final` 带权威 `text` 和 `citations`，前端必须替换草稿，不能仅追加。已经展示过的草稿并不会因替换而变成“从未暴露”。

识别和证据自评的模型请求不会直接逐字转发给浏览器。浏览器协议还包含进度、重放、失败和取消边界，见 [FastAPI](FastAPI.md) 与 [Agent 实现详解](Agent实现详解.md)。

## 7. 按一次请求读源码

| 问题 | 文件与符号 |
| --- | --- |
| 模型如何装配 | [models.py](../backend/app/agent/models.py)：`create_models`、`model_configuration_error` |
| 识别输入和校验 | [nodes/intent.py](../backend/app/agent/nodes/intent.py)：`_structured_messages`、`make_recognize_intent`、`route_intent` |
| 工具从哪里来 | [registry.py](../backend/app/services/tools/registry.py)：`SUPPORT_TOOLS` |
| 谁构造调用 | [nodes/tools.py](../backend/app/agent/nodes/tools.py)：`make_dispatch_tool_call`、`after_tools` |
| 如何逐片段生成与核验引用 | [nodes/answer.py](../backend/app/agent/nodes/answer.py)：`make_generate` |
| 图如何执行 | [graph.py](../backend/app/agent/graph.py)：`build_support_graph` |
| 如何保存再发布权威答案 | [nodes/context.py](../backend/app/agent/nodes/context.py)：`make_save_answer`；[conversations.py](../backend/app/api/routes/conversations.py)：`stream_message` |

## 8. 排查与进阶

- 客户端能创建但请求失败：先区分缺配置、认证、模型名与兼容接口参数问题，不把固定错误文案当模型答复。
- JSON 合法却没有工具执行：看 Pydantic 字段校验、原话来源和服务端授权；不要以提高模型置信度绕过权限。
- 知识工具报错：查 embedding、Milvus、rerank 的失败阶段；当前实现不静默转 MySQL `LIKE`。
- 流有草稿但最终拒答：检查引用编号是否属于本轮证据；编号子集校验也不等于逐事实语义证明。
- callbacks/metadata 可用于观测执行，不能把依赖调用记录当真实服务验收。

进一步学习 Runnable 组合、结构化输出和自主工具 Agent 时，要把“框架支持”与“本项目采用”分开。官方入口：[LangChain 文档](https://docs.langchain.com/oss/python/langchain/overview)、[消息](https://docs.langchain.com/oss/python/langchain/messages)、[工具](https://docs.langchain.com/oss/python/langchain/tools)。
