# Aiden MySQL 数据层

本地 MySQL 使用 MySQL 8.4，ORM 使用 SQLAlchemy 2.x，迁移使用 Alembic。数据层代码位于 backend/app/persistence/mysql/。导入模型或会话模块不会创建数据库表；表结构只由 Alembic 迁移管理。

## 表与关系

| 表 | 主要字段与用途 | 关系 |
| --- | --- | --- |
| **users** | **id**、**name**、唯一 **email**、可选且唯一的 **phone**、**role**、可选 **password_salt/password_hash** | 用户拥有订单和会话；客服账号可被指定为会话、工单负责人 |
| **login_sessions** | **token_hash**、**user_id**、**expires_at**、**created_at** | 只保存 HttpOnly Cookie 令牌的 SHA-256 哈希；按用户外键级联清理 |
| **products** | **name**、**category**、**description**、**is_active** | 一个商品有多个 SKU |
| **product_skus** | 商品外键、唯一 **sku_code**、规格 JSON、**price**、**stock_quantity**、**is_active** | 属于一个商品；订单行可选关联当前 SKU |
| **orders** | 唯一 **order_no**、**user_id**、**total_amount**、**currency**、**status**、**ordered_at** | 属于一个用户；包含订单行和包裹 |
| **order_items** | **order_id**、可空 **sku_id**、商品/SKU 名称与规格快照、**quantity**、**unit_price**、**line_total** | 属于一个订单；删除 SKU 后保留订单快照 |
| **shipments** | **order_id**、包裹号、承运商、运单号、配送 **status**、发货/签收时间 | 一个订单可有多个包裹 |
| **tracking_events** | **shipment_id**、节点代码、状态、地点、描述、**occurred_at** | 属于一个包裹；按包裹和发生时间索引 |
| **after_sale_requests** | 唯一申请号、用户/订单/订单行、申请类型、原因、处理状态、申请金额 | 复合外键确保用户确为订单所有者、订单行确属该订单 |
| **policies** | **policy_key**、**name**、**version**、正文、**effective_from/effective_until**、**is_active** | **(policy_key, version)** 唯一；可保留同一政策的多个版本 |
| **faq** | 分类、问题、答案、**is_active** | 独立问答条目 |
| **conversations** | **user_id**、主题、状态、接管客服、开始/结束时间、**last_message_preview** | 属于一个用户；复合唯一键供工单校验归属 |
| **messages** | **conversation_id**、发送者角色、内容、发送状态、**client_message_id**、**created_at** | 属于一个会话；删除会话时级联删除消息；客户端重试键不作为主键 |
| **tickets** | 唯一工单号、会话/用户、问题类型与描述、状态、负责人、解决时间 | 复合外键保证工单的用户与会话所有者一致 |
| **unanswered_questions** | 会话、问题、可选置信度、审核状态、审核人/备注/时间 | 关联产生该问题的会话，可记录人工审核结果 |

用户、商品、订单、会话等主键采用应用生成的 UUID 字符串（36 字符）。外键默认限制删除有业务记录的用户、商品和订单；消息及物流节点随其会话或包裹清理。关键关联列均有索引，订单号、工单号、SKU 编码、邮箱、手机号和政策键/版本有唯一约束。订单商品保存购买时的商品名、规格、SKU 编码和成交价，目录数据后续变化不会改写订单历史。

## 字段约定

- 金额用 DECIMAL(12, 2)，ORM 类型标注为 decimal.Decimal；订单保存币种，默认 CNY。SKU 当前售价与订单成交价分开存储。
- MySQL DATETIME 不携带时区；应用将时间按 UTC 写入，并在读取/展示边界转换时区。政策有效期采用左闭右开区间：effective_from <= 查询时间 < effective_until，结束时间为空表示无截止日期。
- 用户密码只存盐和哈希字段，不存明文；二者可空，以支持由外部身份系统管理凭据的账号。手机号允许空值，MySQL 对唯一索引中的多个 NULL 不视为冲突。
- orders.status、包裹状态和政策内容不做固定枚举约束，便于接入外部订单状态和后续政策格式；会话、消息、售后、工单、问题池等内部流程状态有检查约束。
- after_sale_requests 的 user_id、order_id 和 order_item_id 通过复合外键保持所属关系一致。工单也通过 (conversation_id, user_id) 复合外键校验所有者。
- unanswered_questions 必须关联一个会话；问题审核状态包含待审核、已审核、已补入 FAQ 和忽略。
- 前端消息的 `role` 映射到 MySQL 的 `sender_role`；`client_message_id` 最长 100 字符，与 36 字符消息主键分开保存，并按会话和发送角色唯一约束。用户消息与流式助手消息在一个短事务中创建。
- 消息时间使用 `DATETIME(6)` 保存微秒，保留用户消息与对应助手占位消息之间的 1 微秒顺序，避免同一秒内由随机 UUID 决定显示位置。
- 所有 MySQL `DATETIME` 按 UTC 写入；API 在返回边界补上 UTC 时区并序列化为 ISO 8601（`Z`）字符串。

## Alembic 的作用

SQLAlchemy 模型描述代码当前期望的表结构；Alembic 负责把结构变化保存成有序、可复用的迁移版本。这样新建数据库或部署到新环境时，可以按同一顺序创建表；以后新增列、索引或表时，也能明确记录这次结构变更，而不依赖服务启动时临时建表。

执行迁移后，Alembic 会在数据库中维护 alembic_version 表，记录已应用到哪个迁移版本。执行 upgrade head 时，它会从当前版本开始，依次执行尚未应用的迁移，直到项目中的最新版本。Aiden 当前先执行 0001 初始结构，再执行 0002 对话服务字段变更，最后执行 0003 消息时间精度修复。

新增结构变更时，应创建一份新的迁移脚本并检查其 upgrade 和 downgrade 操作，再对目标数据库执行迁移。已有迁移作为历史记录保留；修改 ORM 模型本身不会自动修改数据库表。downgrade 可以按迁移脚本尝试回退结构，但不能代替数据备份，且删除列等回退操作可能丢失数据。

常用命令（在 backend 目录，且已设置 DATABASE_URL）：

    # 应用所有尚未执行的迁移
    .\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
    # 为后续结构变更生成迁移草稿，生成后需检查脚本再应用
    .\.venv\Scripts\alembic.exe -c alembic.ini revision --autogenerate -m "describe schema change"
    # 按迁移脚本回退一个版本；操作前应确认数据影响
    .\.venv\Scripts\alembic.exe -c alembic.ini downgrade -1

## 本机启动与迁移（PowerShell）

以下命令从仓库根目录运行。后端需要 Python 3.10 或更新版本；若本机 `python` 指向旧版本，请在命令中换成已安装的较新解释器。示例文件只有占位密码；复制后请在本机编辑 deploy/.env.mysql，并让 DATABASE_URL 中的用户名、密码、数据库名和端口与 MySQL 变量一致。真实密码不要提交。若密码包含 URL 保留字符，需在 DATABASE_URL 中按 URL 规则编码。

    Copy-Item deploy/.env.mysql.example deploy/.env.mysql
    # 编辑 deploy/.env.mysql 中的 MYSQL_PASSWORD、MYSQL_ROOT_PASSWORD 和 DATABASE_URL
    docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml up -d mysql
    docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml ps
    Set-Location backend
    # requirements.txt 同时安装 API、Agent 与 MySQL/Alembic 运行依赖
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    $urlLine = Get-Content ..\deploy\.env.mysql | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1
    $env:DATABASE_URL = $urlLine.Substring('DATABASE_URL='.Length)
    .\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
    # 创建本地演示用户（已存在且无冲突的账号会保留）
    .\.venv\Scripts\python.exe -m scripts.init_demo_users
    # 启动 FastAPI（DATABASE_URL 沿用当前 PowerShell 会话）
    .\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

关闭容器（命名数据卷保留，下次启动可继续使用）：

    Set-Location ..
    docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml down

deploy/.env.mysql 是本机配置，不要提交；应提交的只有 deploy/.env.mysql.example。Compose 只启动 MySQL 服务，不包含 FastAPI。

## 后续服务接入入口

从 backend 目录导入：

- app.persistence.mysql.get_session：可作为 FastAPI Depends 使用的会话生成器；事务提交由调用方决定。
- app.persistence.mysql.session_scope：上下文管理器，成功时提交，异常时回滚。
- app.persistence.mysql.get_engine / get_session_factory：按进程环境中的 DATABASE_URL 延迟创建并缓存 Engine/Session 工厂。
- app.persistence.mysql.models：15 张表对应的 ORM 类及 Base。
- app.persistence.mysql.queries：list_user_orders(session, user_id)、list_order_tracking_events(session, user_id, order_id)、get_effective_policy(session, policy_key, at=None)、list_conversation_messages(session, user_id, conversation_id)。

订单、物流轨迹、会话消息三个查询都在 SQL 条件中要求 user_id，不会只凭订单号或会话 ID 返回其他用户的数据。

## 客服只读工具

客服 Agent 已通过 `backend/app/services/tools/` 接入三个只读查询工具，完整参数、返回字段、权限边界和流式行为见 [`tools.md`](tools.md)：

- `query_order` 从当前用户的订单中按订单号精确筛选；未提供订单号时最多返回最近 5 笔，并带订单商品快照。
- `query_logistics` 先按 `user_id` 和订单号确认订单归属，再经订单关系读取包裹和物流节点。未知订单号和其他用户的订单号都返回空结果。
- `search_faq` 只检索 `faq.is_active = true` 的记录，最多返回 5 条匹配的问题、答案和分类。

身份由会话路由从认证 Cookie 解析后写入 LangGraph 状态，再通过 `InjectedState` 注入订单和物流工具；模型可见的工具参数不包含 `user_id`。工具使用 SQLAlchemy 查询和短生命周期 Session，不修改数据，也不需要新增表或迁移。当前 FAQ 表没有全文索引，工具使用有界的关键词 `LIKE` 匹配，不是语义检索。

## FastAPI 对话服务接入

- 登录、Cookie 会话、会话列表、消息历史、发消息和 LangGraph 上下文均使用 MySQL；服务启动不会自动建表或修改表结构。先运行 Alembic，再启动 API。
- Agent 模型绑定上述只读工具；LangGraph 按“生成工具调用、执行工具、将结果交回模型、保存最终回答”的循环运行。订单、物流和 FAQ 的数据必须来自工具结果，查无记录时回答空结果；售后申请、政策表和工单尚无对应查询工具。
- `0002_chat_runtime` 是新增迁移，保留 `0001_initial` 不变。它增加登录会话表、消息重试键和会话预览字段。
- `0003_message_timestamp_precision` 将消息时间改为微秒精度，并修正已有用户消息与助手回复的成对顺序。
- 普通用户演示登录为 `maya@aiden.demo` / `chen@aiden.demo`，密码均为 `aiden123`。执行 `python -m scripts.init_demo_users` 可显式创建账号；它只补入不存在且无冲突的记录，不会重置已有密码。账号与目标库现有记录冲突时会停止并回滚。
- 应用 Alembic 迁移后，在 `backend` 目录执行 `python -m scripts.init_demo_users` 创建本地演示账号。该命令只补入缺少且无冲突的账号，不会重置已有密码或业务数据。
- 复制 `backend/.env.example`（只在本机尚无 `backend/.env` 时复制），在其中填写模型 API Key、Base URL、模型名及 `DATABASE_URL`。DeepSeek Flash 示例使用 `OPENAI_BASE_URL=https://api.deepseek.com`、`OPENAI_MODEL=deepseek-flash`。`DATABASE_URL` 应与 `deploy/.env.mysql` 中的数据库名、账号和端口相同；密钥和本地密码不要提交。
- 前端保持 remote 模式并在 `frontend` 目录执行 `npm run dev`；后端 API 默认监听 `127.0.0.1:8000`。不需要修改前端 SSE 客户端。Mock 模式仍可独立使用。
- 每个数据库读写函数只在单次短操作内创建并关闭 SQLAlchemy Session；不会跨越模型流式 `await` 持有同步 Session。会话、消息、订单和物流查询都在 SQL 条件或已校验的 ORM 关系中限制当前用户归属。
- SSE 继续使用 `start`、`delta`、`done` 和 `error` 事件。模型生成轮的文本片段先暂存；确认该轮没有工具调用后才作为 `delta` 发出，以免把工具计划显示为客服回答。
