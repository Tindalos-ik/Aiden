# Aiden 客服工具系统

本文记录当前后端实际接入的工具、意图路由和执行边界。工具通过 [`registry.py`](../backend/app/services/tools/registry.py) 静态登记，由 [`graph.py`](../backend/app/agent/graph.py) 装配为 LangGraph 节点；目前没有 MCP Server 或运行时动态发现。

## 已注册工具

| 工具 | 服务端传入的业务参数 | 数据和结果 | 限制 |
| --- | --- | --- | --- |
| `query_order` | 可选 `order_no` | 本人订单、状态、金额和购买时的商品快照 | 不传订单号时最近最多 5 笔 |
| `query_logistics` | 必填 `order_no` | 本人订单的包裹、承运商和物流节点 | 先按本人订单校验归属 |
| `search_faq` | 必填 `question` | Milvus 检索已向量化知识块，再回 MySQL 取得原文和引用信息 | 商品和政策咨询、退款办理前政策核对；结果须与问题相符才能回答 |
| `query_after_sale` | 可选 `request_no`、`order_no` | 本人退款、退货、换货申请的类型、状态、原因、金额和时间 | 两编号同时传入时取交集；不传时最近最多 5 条 |
| `create_ticket` | `issue_type`、`description` | 在 `tickets` 表登记一条状态为 `open` 的工单，返回工单号和是否已存在 | 类型限退款退货、售后、投诉、人工请求；退款类型还要求图状态中的明确登记授权 |
| `query_ticket` | 可选 `ticket_no` | 本人工单的类型、描述、状态及创建、解决时间 | 不传工单号时最近最多 5 条 |

`query_order`、`query_logistics`、`query_after_sale`、`query_ticket` 的 `user_id` 从已认证的 LangGraph 状态注入，模型不能填写。`create_ticket` 还注入 `conversation_id`、`assistant_message_id` 和退款登记授权。`search_faq` 读取公共知识，不按用户过滤。工具实现在 [`services/tools`](../backend/app/services/tools/)，订单、售后和工单查询位于 [`queries.py`](../backend/app/persistence/mysql/queries.py)，工单写入位于 [`ticket_write.py`](../backend/app/persistence/mysql/ticket_write.py)。当前注册表没有 `query_policy`，`policies` 表查询函数未接入客服工具。

## 单轮多请求如何处理

`recognize_intent` 一次把当前消息拆成按用户表达顺序排列的 `requests`，每项都有固定意图、处理目标、独立补全问题、业务编号、置信度和澄清状态。Pydantic 校验字段、枚举和最多四项的上限；超出上限会要求用户分批发送。不能识别的单项保留在答复中并要求补充，其他项继续处理。相同诉求即使被重复识别，相同查询参数的只读工具调用也只执行一次。

图按列表顺序逐项路由和执行：订单/物流先做原有订单号来源或本人近期订单列表核验，再执行当前项获准工具；售后申请、政策、工单和商品知识可在同一轮分别查询。每项工具结束后收回权限，再处理下一项。某项缺编号、查无结果、工具报错或知识证据不足，不中止其他独立请求。最终答复按原顺序逐项给出结果或明确的澄清、空结果、失败说明；商品知识仍校验引用编号。

## 九类意图与目标

| 意图 | 当前处理方式 |
| --- | --- |
| `order` | `query_order`；目标不明确时先列本人近期订单供选择 |
| `logistics` | `query_logistics`；必要时先用 `query_order` 核验本人订单目标 |
| `product` | `search_faq`，根据实际召回证据回答并标注引用 |
| `refund_return` | 查规则用 `search_faq`；查已提交申请用 `query_after_sale`；查已有工单用 `query_ticket`；要求办理时先核对本人订单、原因和政策，再询问是否登记待处理工单 |
| `after_sales` | 查规则用 `search_faq`；查售后申请或工单分别用 `query_after_sale`、`query_ticket`；要求处理时登记工单 |
| `complaint` | 查已有工单用 `query_ticket`，要求处理时用 `create_ticket` |
| `human` | 查已有人工请求工单用 `query_ticket`；明确要求转人工时进入会话人工队列，不调用 `create_ticket` |
| `smalltalk` | 直接简短回答，不调用业务工具 |
| `other` 或低置信度 | 说明支持的服务，请用户补充问题，不开放工具 |

拆分器只能返回固定意图和目标，不能指定工具名或用户身份。`route_intent` 对每项检查意图与目标的允许组合，在服务端计算单次工具白名单；`dispatch_tool_call` 构造参数并按工具名与参数复用本轮相同查询结果。退款办理目标虽然沿用 `create_ticket` 枚举，首次表达“我要退款”或“帮我登记退款处理”只进入核对流程：缺订单时列本人近期订单，核对订单后缺原因则追问，原因齐备后检索适用政策并验证引用；证据不足时不展示登记确认。证据足够时说明已知条件、仍需核实的信息，以及只能登记待处理工单。服务端把核对阶段保存在上一条助手消息的 `workflow_state` 中；只有紧邻上一轮的可信状态为待确认且本轮用户明确同意，才开放退款写入。可见文字本身不能授权写入；拒绝时不写入。同一轮至多登记一项，客户端同一消息重试由助手消息 ID 幂等重放。只有最终回答写入助手消息；SSE 仍使用 `start`、`delta`、`done`、`error`，知识引用另用 `citations` 事件。

## 数据和操作边界

- 订单、售后申请和工单查询在 SQL 中同时按当前用户过滤。不存在与属于其他用户的业务编号均返回相同的空结果；工具不向模型返回用户 ID、数据库主键或员工 ID。
- 政策回答依据 `search_faq` 本轮召回的知识块及引用；检索失败、证据不足或政策条件无法确认时不编造退款资格、金额或时效。`policies` 表的有效版本查询函数虽存在，但不是当前 Agent 的工具，不能把它描述成已执行的政策核验。
- `create_ticket` 写入前核验会话及助手消息都属于当前用户，用助手消息 ID 派生稳定工单号；退款类型还检查图状态中的确认授权，并在数据库事务中按用户串行检查同订单开放工单，避免跨消息重复建单。它只创建工单，不创建退款退货申请，也不把会话改成 `waiting` 或 `staff`。当前 remote 后端尚无人工接单流程，因此不能宣称真人已经接入或保证处理时间。
- `messages.workflow_state` 由迁移 `0007_refund_dialogue_state` 新增，仅保存已核对的退款对话阶段、订单号和原因；消息 API 不返回此字段。回复成功入库时与助手文本同一事务保存，下一轮只接受紧邻本轮用户消息的上一条助手状态。
- 数据库 Session 只在各工具的短操作内打开。数据库、向量服务或 Milvus 异常转成通用错误结果；`search_faq` 不在向量服务故障时静默退回关键词检索。错误结果不包含 SQL、堆栈或连接信息。

数据库结构与演示数据见 [`数据层.md`](数据层.md)；知识检索链路见 [`RAG.md`](RAG.md)；测试样例见 [`测试回答.md`](测试回答.md)。
