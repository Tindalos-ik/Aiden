# 客服 Agent 只读工具

本文说明已接入的订单、物流和知识检索工具。工具实现位于 [`backend/app/services/tools/customer.py`](../backend/app/services/tools/customer.py)，订单与物流查询位于 [`backend/app/persistence/mysql/queries.py`](../backend/app/persistence/mysql/queries.py)，知识检索链路位于 [`backend/app/services/rag/retrieval.py`](../backend/app/services/rag/retrieval.py) 与 [`backend/app/persistence/milvus/knowledge_store.py`](../backend/app/persistence/milvus/knowledge_store.py)。数据库表结构和本地准备方法见 [`database-schema.md`](database-schema.md)，演示数据问题见 [`测试回答.md`](测试回答.md)，检索原理与配置见 [`RAG.md`](RAG.md#在线语义检索)。

## 工具一览

| 工具 | 模型可传参数 | 查询范围 | 空结果 |
| --- | --- | --- | --- |
| `query_order` | `order_no` 可选 | 当前登录用户的订单及商品快照；省略订单号时按下单时间倒序最多返回 5 笔 | 返回 `found: false`、空 `orders` 列表及说明 |
| `query_logistics` | `order_no` 必填 | 当前登录用户指定订单的包裹、承运信息和物流节点 | 不存在或不属于当前用户的订单返回 `found: false`；有订单但无包裹时返回 `found: true` 和空 `shipments` 列表 |
| `search_faq` | `question` 必填 | 已向量化的知识库（商品 FAQ、政策、手册、对话挖掘问答）：问题经 BGE dense 模型编码后在 Milvus 召回，再回 MySQL 取权威原文；默认最多返回 5 条 | 返回空 `results` 列表及说明 |

订单结果只含订单号、状态、总金额、币种、下单时间和商品名称、规格名称、数量、成交价。物流结果只含包裹号、承运商、运单号、状态、发货/签收时间和节点状态、说明、地点、发生时间。`search_faq` 每条结果只含 `category`、`question`、`answer`。工具不返回地址、手机号、数据库主键、向量或相似度分数。

MySQL `DATETIME` 按 UTC 约定写入；工具把返回时间标为 ISO 8601 UTC（末尾 `Z`）。金额以十进制字符串返回，避免 JSON 浮点数改变金额精度。

## LangGraph 调用流程

```mermaid
graph TD
    auth["认证用户消息"] --> context["加载会话上下文"]
    context --> intent["语义识别：意图、补全问题、实体"]
    intent --> route["服务端计算工具白名单"]
    route --> decision{"白名单里有工具吗"}
    decision -->|有| dispatch["服务端构造工具调用"]
    dispatch --> execute["ToolNode 执行对应只读工具"]
    execute --> tools_after["收回工具权限"]
    tools_after --> answer["生成最终回答"]
    decision -->|没有| answer
    answer --> save["保存最终回答"]
    save --> response["发送 SSE delta 和 done"]
```

工具名与参数由服务端决定，不由模型提交：`route_intent` 按固定意图集合算出 `allowed_tools`，`dispatch_tool_call` 只用服务端已核验的实体构造调用（`search_faq` 传补全后的当前问题，`query_order`/`query_logistics` 传已授权订单号），`ToolNode` 再执行对应工具并把 `ToolMessage` 交回模型。工具调用轮不保存为助手回答，只有最终回答会写入原有助手消息记录。

## 身份、查询和错误约束

- HTTP 路由通过 `current_actor` 验证登录 Cookie，再把服务端得到的 `actor.id` 写入 LangGraph 状态。订单与物流工具使用 `InjectedState("user_id")` 读取该值；OpenAI 工具参数 Schema 不包含 `user_id`，模型不能指定查询身份。
- `query_order` 的 SQL 同时匹配 `Order.user_id` 和可选的精确订单号。最近订单查询也必须有 `user_id` 条件。
- `query_logistics` 先以 `user_id` 和订单号找到用户拥有的订单，再通过订单关系加载包裹与轨迹。不同用户的订单号与不存在的订单号得到相同的空结果。
- 知识库是商城公共内容，不按用户隔离；`search_faq` 只接受 `knowledge_chunks.vector_status = 'vectorized'` 的块，Milvus 中命中但状态不符、原文已失效或 MySQL 已无对应记录的候选一律丢弃。
- 订单与物流查询使用 SQLAlchemy 表达式和绑定参数；`search_faq` 不拼 SQL，只按主键批量回表，且只取 `vectorized` 状态。
- 每个工具在短时间内创建并关闭自己的 SQLAlchemy Session，Milvus 检索期间不持有 MySQL 连接。
- 数据库查询异常由工具转成通用错误结果；`search_faq` 在向量服务或 Milvus 不可用时同样返回通用错误结果，不退回关键词查询；未预期的工具节点异常由 SSE 路由转换为通用错误事件。错误响应不包含堆栈、SQL、连接信息或数据库异常内容。

## 回答边界与 SSE

系统提示要求订单、物流和知识类答案必须依据工具结果。工具返回空列表时，模型应说明没有匹配记录；查询报错时应说明暂时无法查询；知识结果与用户问题对不上时同样要说明暂时无法核实。退款/售后申请、政策版本与工单目前没有单独查询工具；只有 `search_faq` 返回的知识内容可以作为常见问题与政策依据，不能从未查询的数据表或模型记忆补出具体状态或条款细节。

消息流仍使用原有 `start`、`delta`、`done`、`error` 事件及其 JSON 字段。路由只收集 `langgraph_node == "generate"` 的模型片段，并暂存到该节点结束；确认该轮没有工具调用后才发送 `delta`。因此工具调用计划不会显示给用户；最终模型轮的片段会在该轮生成完成后发送，客户端仍按原方式拼接文本。
