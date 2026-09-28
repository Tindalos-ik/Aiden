# Aiden 客服工具系统

本文记录当前后端实际接入的工具、意图路由和执行边界。工具通过 [`registry.py`](../backend/app/services/tools/registry.py) 静态登记，由 [`graph.py`](../backend/app/agent/graph.py) 装配为 LangGraph 节点；目前没有 MCP Server 或运行时动态发现。

## 已注册工具

| 工具 | 服务端传入的业务参数 | 数据和结果 | 限制 |
| --- | --- | --- | --- |
| `query_order` | 可选 `order_no` | 本人订单、状态、金额和购买时的商品快照 | 不传订单号时最近最多 5 笔 |
| `query_logistics` | 必填 `order_no` | 本人订单的包裹、承运商和物流节点 | 先按本人订单校验归属 |
| `search_faq` | 必填 `question` | Milvus 检索已向量化知识块，再回 MySQL 取得原文和引用信息 | 商品咨询使用；结果须与问题相符才能回答 |
| `query_after_sale` | 可选 `request_no`、`order_no` | 本人退款、退货、换货申请的类型、状态、原因、金额和时间 | 两编号同时传入时取交集；不传时最近最多 5 条 |
| `query_policy` | 必填 `question` | `policies` 表中的名称、版本、正文和生效区间 | 仅返回当前 UTC 时间有效且启用的相关政策，最多 5 条 |
| `create_ticket` | `issue_type`、`description` | 在 `tickets` 表登记一条状态为 `open` 的工单，返回工单号 | 类型限退款退货、售后、投诉、人工请求；同一助手消息重复执行返回同一工单 |
| `query_ticket` | 可选 `ticket_no` | 本人工单的类型、描述、状态及创建、解决时间 | 不传工单号时最近最多 5 条 |

`query_order`、`query_logistics`、`query_after_sale`、`query_ticket` 的 `user_id` 从已认证的 LangGraph 状态注入，模型不能填写。`create_ticket` 还注入 `conversation_id` 和 `assistant_message_id`。`query_policy` 和 `search_faq` 读取公共内容，不按用户过滤。工具实现在 [`services/tools`](../backend/app/services/tools/)，订单、售后、政策和工单查询位于 [`queries.py`](../backend/app/persistence/mysql/queries.py)，工单写入位于 [`ticket_write.py`](../backend/app/persistence/mysql/ticket_write.py)。

## 单轮多请求如何处理

`recognize_intent` 一次把当前消息拆成按用户表达顺序排列的 `requests`，每项都有固定意图、处理目标、独立补全问题、业务编号、置信度和澄清状态。Pydantic 校验字段、枚举和最多四项的上限；超出上限会要求用户分批发送。不能识别的单项保留在答复中并要求补充，其他项继续处理。相同诉求即使被重复识别，相同查询参数的只读工具调用也只执行一次。

图按列表顺序逐项路由和执行：订单/物流先做原有订单号来源或本人近期订单列表核验，再执行当前项获准工具；售后申请、政策、工单和商品知识可在同一轮分别查询。每项工具结束后收回权限，再处理下一项。某项缺编号、查无结果、工具报错或知识证据不足，不中止其他独立请求。最终答复按原顺序逐项给出结果或明确的澄清、空结果、失败说明；商品知识仍校验引用编号。

## 九类意图与目标

| 意图 | 当前处理方式 |
| --- | --- |
| `order` | `query_order`；目标不明确时先列本人近期订单供选择 |
| `logistics` | `query_logistics`；必要时先用 `query_order` 核验本人订单目标 |
| `product` | `search_faq`，根据实际召回证据回答并标注引用 |
| `refund_return` | 查规则用 `query_policy`；查已提交申请用 `query_after_sale`；查已有工单用 `query_ticket`；要求办理时用 `create_ticket` 登记诉求 |
| `after_sales` | 查规则用 `query_policy`；查售后申请或工单分别用 `query_after_sale`、`query_ticket`；要求处理时登记工单 |
| `complaint` | 查已有工单用 `query_ticket`，要求处理时用 `create_ticket` |
| `human` | 查已有人工请求工单用 `query_ticket`，要求人工协助时用 `create_ticket` |
| `smalltalk` | 直接简短回答，不调用业务工具 |
| `other` 或低置信度 | 说明支持的服务，请用户补充问题，不开放工具 |

拆分器只能返回固定意图和目标，不能指定工具名或用户身份。`route_intent` 对每项检查意图与目标的允许组合，在服务端计算单次工具白名单；`dispatch_tool_call` 构造参数并按工具名与参数复用本轮相同查询结果。`create_ticket` 仅在用户原文明确要求投诉处理、人工协助或办理售后时开放，同一轮至多登记一项；客户端同一消息重试由助手消息 ID 幂等重放。多个查询不会自动触发写入。只有最终回答写入助手消息；SSE 仍使用 `start`、`delta`、`done`、`error`，知识引用另用 `citations` 事件。

## 数据和操作边界

- 订单、售后申请和工单查询在 SQL 中同时按当前用户过滤。不存在与属于其他用户的业务编号均返回相同的空结果；工具不向模型返回用户 ID、数据库主键或员工 ID。
- 政策查询同时要求 `is_active = true` 和当前时间落在 `[effective_from, effective_until)`；查不到相关现行版本时不能引用过期条款。`search_faq` 的知识库与 `policies` 表是不同来源；知识结果带引用编号及用于来源展示的块元数据。
- `create_ticket` 写入前核验会话及助手消息都属于当前用户，用助手消息 ID 派生稳定工单号；并发或重试不会为同一消息重复建单。它只创建工单，不创建退款退货申请，也不把会话改成 `waiting` 或 `staff`。当前 remote 后端尚无人工接单流程，因此不能宣称真人已经接入或保证处理时间。
- 数据库 Session 只在各工具的短操作内打开。数据库、向量服务或 Milvus 异常转成通用错误结果；`search_faq` 不在向量服务故障时静默退回关键词检索。错误结果不包含 SQL、堆栈或连接信息。

数据库结构与演示数据见 [`数据层.md`](数据层.md)；知识检索链路见 [`RAG.md`](RAG.md)；测试样例见 [`测试回答.md`](测试回答.md)。
