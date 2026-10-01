# Aiden 客服工具系统

本文记录当前后端实际接入的工具、意图路由和执行边界。工具通过 [`registry.py`](../backend/app/services/tools/registry.py) 静态登记，由 [`graph.py`](../backend/app/agent/graph.py) 装配为 LangGraph 节点；目前没有 MCP Server 或运行时动态发现。

## 已注册工具

| 工具 | 服务端传入的业务参数 | 数据和结果 | 限制 |
| --- | --- | --- | --- |
| `query_order` | 可选 `order_no` | 本人订单、状态、金额和购买时的商品快照 | 不传订单号时最近最多 5 笔 |
| `query_logistics` | 必填 `order_no` | 本人订单的包裹、承运商和物流节点 | 先按本人订单校验归属 |
| `search_faq` | 必填 `question` | Milvus 检索已向量化知识块，再回 MySQL 取得原文和引用信息 | 商品与政策咨询；结果须与问题相符才能回答。检索结果只是说明材料，不构成提交退款的依据 |
| `query_after_sale` | 可选 `request_no`、`order_no` | 本人退款、退货、换货申请的类型、状态、原因、金额和时间 | 两编号同时传入时取交集；不传时最近最多 5 条 |
| `query_refund_policy` | 服务端核验的 `order_no` | 本人订单当前有效商家退款政策的 ID、名称、版本、正文与 `snapshot` | 只读；订单号来自服务端已核验上下文，模型不能指定；快照绑定政策内容及生效边界 |
| `submit_after_sale` | 服务端核验的 `order_no`、`reason`、`policy_id`、`policy_snapshot` | 提交正式退款申请（`after_sale_requests.policy_reference` 记为政策 ID），返回申请号与待审核状态 | 仅单商品订单；上一条助手保存同订单、原因、政策 ID 和快照的确认，本轮用户原文为“确认提交”；写入层复核当前政策及快照，同 ID 修改也拒绝旧确认 |
| `create_ticket` | `issue_type`、`description` | 在 `tickets` 表登记一条状态为 `open` 的工单，返回工单号和是否已存在 | 仅用于投诉、无法直接办理的售后异常及人工请求，不代表售后申请 |
| `query_ticket` | 可选 `ticket_no` | 本人工单的类型、描述、状态及创建、解决时间 | 不传工单号时最近最多 5 条 |

`query_order`、`query_logistics`、`query_after_sale`、`query_refund_policy`、`query_ticket` 的 `user_id` 从已认证的 LangGraph 状态注入，模型不能填写。`submit_after_sale` 还注入会话、助手消息和确认授权；服务端写入层独立校验上一轮保存的确认状态（阶段、订单号、原因、政策 ID 与政策快照）、本轮确认原文、政策当前有效且完整快照仍匹配、订单与商品归属。同 ID 原地修改政策也须重新核验并再次确认。`search_faq` 读取公共知识，不按用户过滤。工具实现在 [`services/tools`](../backend/app/services/tools/)，事务写入位于 [`service_workflow.py`](../backend/app/persistence/mysql/service_workflow.py)。`query_refund_policy` 与用户页面的售后预览 API 复用同一个 `preview_request()`，只读本人订单当前有效的退款政策；它也是对话流程读取 `policies` 表的唯一入口，未核验订单不查询。

## 单轮多请求如何处理

`recognize_intent` 一次把当前消息拆成按用户表达顺序排列的 `requests`，每项都有固定意图、处理目标、独立补全问题、业务编号、置信度和澄清状态。Pydantic 校验字段、枚举和最多四项的上限；超出上限会要求用户分批发送。不能识别的单项保留在答复中并要求补充，其他项继续处理。相同诉求即使被重复识别，相同查询参数的只读工具调用也只执行一次。

图按列表顺序逐项路由和执行：订单/物流先做原有订单号来源或本人近期订单列表核验，再执行当前项获准工具；售后申请、政策、工单和商品知识可在同一轮分别查询。每项工具结束后收回权限，再处理下一项。某项缺编号、查无结果、工具报错或知识证据不足，不中止其他独立请求。最终答复按原顺序逐项给出结果或明确的澄清、空结果、失败说明；商品知识仍校验引用编号。

## 九类意图与目标

| 意图 | 当前处理方式 |
| --- | --- |
| `order` | `query_order`；目标不明确时先列本人近期订单供选择 |
| `logistics` | `query_logistics`；必要时先用 `query_order` 核验本人订单目标 |
| `product` | `search_faq`，根据实际召回证据回答并标注引用 |
| `refund_return` | 查规则用 `search_faq`；查已提交申请用 `query_after_sale`；查已有工单用 `query_ticket`；办理退款时核对本人订单、单件商品和用户原因，再用 `query_refund_policy` 取本人订单当前有效的退款政策，只有政策正文覆盖用户陈述的原因才询问是否正式提交申请 |
| `after_sales` | 查规则用 `search_faq`；查售后申请或工单分别用 `query_after_sale`、`query_ticket`；要求处理时登记工单 |
| `complaint` | 查已有工单用 `query_ticket`，要求处理时用 `create_ticket` |
| `human` | 查已有人工请求工单用 `query_ticket`；明确要求转人工时进入会话人工队列，不调用 `create_ticket` |
| `smalltalk` | 直接简短回答，不调用业务工具 |
| `other` 或低置信度 | 说明支持的服务，请用户补充问题，不开放工具 |

拆分器只能返回固定意图和目标，不能指定工具名或用户身份。`route_intent` 对每项检查意图与目标组合，在服务端计算工具白名单；`dispatch_tool_call` 构造参数并复用本轮相同查询结果。售后办理保留 `create_ticket` 内部目标，但首次表达只进入核对，不直接写入。单商品退款先核本人订单、原因和商家政策，给出固定确认文本后，本轮用户原文必须是“确认提交”；写入层复核紧邻助手保存的阶段、订单号、原因、政策 ID 与快照。政策变化使旧确认失效，仅保留订单和原因供用户回复“重新核验”后重新查询并再次确认。无有效政策时仅给带订单及原因的页面政策核对/咨询路径，不承诺页面能提交，并记录 `refund_policy_unavailable`。多商品退款、退货和换货核验本人订单后转 `/app/service?orderNo=...&requestType=...`，可附已核验原因；订单追问保留申请类型。拒绝时不写入；客户端同一消息重试由助手消息 ID 幂等重放。只有最终回答写入助手消息；SSE 仍使用 `start`、`delta`、`done`、`error`，知识引用另用 `citations` 事件。

## 数据和操作边界

- 订单、售后申请和工单查询在 SQL 中同时按当前用户过滤。不存在与属于其他用户的业务编号均返回相同的空结果；工具不向模型返回用户 ID、数据库主键或员工 ID。
- 知识类政策回答依据 `search_faq` 本轮召回知识块及引用；检索失败、证据不足或政策条件无法确认时不编造退款资格、金额或时效。申请写入依据仅为有效商家政策及提交时匹配的快照，不接受旧 `rag:` 引用授权。对话退款还要求政策正文覆盖用户原因；无有效政策时对话和页面都不能正式提交。`query_policy` 未注册给对话 Agent，不能描述成它执行了政策核验。
- `create_ticket` 写入前核验会话及助手消息归属，用助手消息 ID 派生稳定工单号。它只创建工单，不把会话改成 `waiting` 或 `staff`。员工工单接手、处理和关闭通过专用 API 完成；会话接管仍是独立流程。
- `messages.workflow_state` 由迁移 `0007_refund_dialogue_state` 新增，保存售后对话阶段、申请类型、已核验订单号与原因；退款确认阶段另保存政策 ID 和快照。消息 API 不返回此字段。回复成功入库时与助手文本同事务保存，下一轮只接受紧邻本轮用户消息的上一条助手状态。页面 POST 必填 `policySnapshot`，详见售后 API 文档。
- 数据库 Session 只在各工具的短操作内打开。数据库、向量服务或 Milvus 异常转成通用错误结果；`search_faq` 不在向量服务故障时静默退回关键词检索。错误结果不包含 SQL、堆栈或连接信息。

数据库结构与演示数据见 [`数据层.md`](数据层.md)；知识检索链路见 [`RAG.md`](RAG.md)；测试样例见 [`测试回答.md`](测试回答.md)。
售后申请与工单 API、状态和外部支付边界见 [`售后与工单.md`](售后与工单.md)。
