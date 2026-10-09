# Aiden 智能订单客服

React + TypeScript + Vite 客户端，默认使用本地 mock 适配器。首次启动不需要 FastAPI，用户、客服、订单、会话和消息均由浏览器本地演示数据提供。

## 启动

```powershell
cd frontend
npm install
npm run dev
```

打开 Vite 输出的本地地址（默认 `http://localhost:5173`）。生产构建与本地预览：

```powershell
npm run build
npm run preview
```

复制 `.env.example` 为 `.env.local` 可显式设置模式。省略 `VITE_API_MODE` 时也默认为 mock。

```dotenv
VITE_API_MODE=mock
VITE_API_BASE_URL=
```

## 演示账号

登录页提供快捷入口，也可以使用下面的账号和密码。mock 登录会按角色进入用户工作台或人工客服工作台。

| 身份 | 账号 | 密码 | 示例订单 |
| --- | --- | --- | --- |
| 普通用户 林沐言 | `maya@aiden.demo` | `aiden123` | 物流中的耳机、已签收的键盘 |
| 普通用户 陈屿 | `chen@aiden.demo` | `aiden123` | 退款中的背包、物流中的咖啡套装 |
| 人工客服 林客服 | `staff@aiden.demo` | `aiden123` | 可查看待接入队列并接管会话 |

推荐现场演示步骤：

1. 用林沐言登录，询问“我的订单到哪了？”，观察“正在查询订单”状态、分段回答和结构化订单卡片。
2. 点击“转人工”，或询问退款争议问题触发转接。
3. 在同一浏览器的另一个标签页用林客服登录，在“待接入”队列打开会话并接入，查看完整对话后发送回复。
4. 切回用户标签页查看接入状态和客服回复。mock 数据写入 `localStorage`，通过 `storage` 事件和 `BroadcastChannel` 同步；当前标签页身份单独保存在 `sessionStorage`。
5. 使用顶部“重置演示”恢复初始数据。输入包含“模拟错误”或“测试错误”的内容可以展示回答失败状态。

mock 的流式输出由浏览器适配器分段生成，不代表已连接模型或订单服务。页面顶部会标明当前模式；只有 remote 模式使用真实 `text/event-stream` SSE。
mock 还预置一张配送投诉工单，可在“售后与工单”页演示查看、员工接单和结单；政策、申请与处理均只在浏览器本地模拟。

## 连接 FastAPI

FastAPI 提供普通用户对话、正式售后申请及工单进度闭环。用户仍从顶部“售后与工单”进入 `/app/service`；员工从侧栏“客户服务 → 售后与工单”进入 `/staff/service`。用户核对订单商品和有效政策后确认提交，可查进度并在允许阶段取消；员工侧可切换工单/售后申请列表，并通过详情抽屉查看和填写处理说明；仅查看或编辑草稿不会提交处理。remote 使用后端 MySQL 和 Cookie 权限，mock 仅在浏览器本地模拟申请及处理，不代表真实退款。员工可从侧栏进入知识运营页面。订单和物流工具是否返回数据取决于后端数据与服务配置。

员工页面使用分组侧边导航：桌面端可折叠侧栏，移动端通过顶部菜单打开导航抽屉；用户端顶部入口保持不变。客服工作台的队列和消息区分别滚动，长对话不会推动页面整体滚动。RAG 建库分为服务与概览、文档预览与导入、入库与向量化任务、对话挖掘、知识块与 Milvus 五区；低置信度评审抽屉默认打开“人工审核”，并可切换原话与召回证据、未核实模型示例；RAG 评估分为总览、分组对比、题目明细和运行配置四区。

remote 用户页中，知识类回答的 `[1]` 等引用编号可点击查看来源 chunk 的章节路径和正文；Markdown 来源提供跳回原文的链接。每段已完成的助手回答下方有“满意 / 不满意”反馈：remote 经 Cookie 鉴权 API 保存至 MySQL 消息行，刷新后从历史消息回显；不满意会由服务端关联真实用户问题并写入低置信度问题池，满意不入池。请求失败会提示错误并允许重试，remote 不回退到 mock。mock 只在浏览器本地演示数据中保存反馈，不代表服务端问题池已写入。

### 员工观测与用量

员工从侧栏“分析观测”分组或 `/staff/observability` 进入；页面按趋势、请求、用量分区读取员工有权查看的真实观测记录，不填入模拟数据。默认只看在线客服；其他来源需手动选择，不会与在线客服混合。可按时间、来源、模型、业务分类、处理状态及会话/消息编号筛选；筛选草稿点击“应用筛选”后生效，刷新保留已应用筛选。相对时间只在打开、选择时间范围或手动刷新时更新；自定义时间刷新仍使用原区间。

页面展示客服请求数、后台处理是否正常完成、平均耗时、模型调用次数、模型用量（Token）、用量分组/趋势及真实调用树，不展示费用。处理状态只代表后台客服处理过程，不代表订单问题已解决或退款到账。模型或业务分类筛选会选择整条请求并保留其中全部调用；用量区域可进一步按真实模型或业务桶下钻。共享意图分类单独展示，不分摊到具体业务。

Token 是模型处理文本的计量单位，不等同于字数。用量按已确认字段分别汇总；完整覆盖要求输入、输出、合计均有记录。缺失字段不补算，历史未核验数据不计入已确认汇总。Token 趋势按唯一模型调用的实际开始 UTC 日、模型和真实归因桶统计，不重复计数。服务内部 Langfuse 费用采集与计量仍保留，但员工页面不展示金额、币种、费用排名或缺金额提示。

若没有匹配记录，可手动缩小到最近 1 小时或 24 小时、扩大到 7 天或 31 天、清除已应用筛选，或点击刷新；页面不会自动改变时间或来源。读取不完整时结果仅代表已读取部分；概览、列表或详情读取失败会明确提示，不会以空结果或零代替。请求详情保留真实记录编号供追溯；只显示已记录的处理步骤，并行步骤不会被伪造成先后顺序。

若提示“观测接口版本与页面不一致”，说明后端未返回当前页面需要的 Token 趋势字段。更新后端源码后，应重启 API，再刷新浏览器；本地启动脚本使用稳定进程，不会自动加载后端代码变化。不兼容响应会显示读取失败提示，保留导航和筛选，不再导致白屏，也不会补造空趋势或零用量。

设置：

```dotenv
VITE_API_MODE=remote
VITE_API_BASE_URL=/api
```

开发服务器默认将 `/api` 代理到 `http://127.0.0.1:8000`。使用根目录 `start.ps1` 时，可在启动前设置 `FASTAPI_PORT`，脚本会用该端口启动后端，并让 Vite 代理自动指向同一端口：

```powershell
$env:FASTAPI_PORT = '8100'
.\start.ps1
```

单独运行前后端时，后端的 Uvicorn 端口和前端启动环境中的 `VITE_API_PROXY_TARGET` 要保持一致，例如将后者设为 `http://127.0.0.1:8100`。独立运行前先确认所选 API 端口未被监听，且不与 embedding 服务端口 `8001` 冲突；`8100` 仅为示例，不保证本机空闲。remote 请求全部使用 `credentials: 'include'`，登录态只从服务端 HttpOnly Cookie 恢复。若改为跨域直连，服务端还需要允许对应前端来源并启用凭证 CORS。remote 接口报错会直接呈现给用户，不会切回 mock。

### 本地启动

以下是 **remote 演示业务基线准备步骤**，不是已完成全链路验收记录。默认前端仍是 mock；根目录 `start.ps1` 会在进程环境**强制**设置 `VITE_API_MODE=remote`、`VITE_API_BASE_URL=/api` 和代理目标，remote 失败不会回退 mock。

#### 1. 依赖和写入边界（先读，再显式执行）

需要 Python 3.10+、Node.js/npm、Docker Desktop/MySQL 8.4、Milvus 2.5+、embedding 服务、reranker 权重和可用的聊天模型。只复制不存在的配置，绝不覆盖已有文件：

```powershell
# 仓库根目录
if (!(Test-Path deploy/.env.mysql)) { Copy-Item deploy/.env.mysql.example deploy/.env.mysql }
if (!(Test-Path backend/.env)) { Copy-Item backend/.env.example backend/.env }
if (!(Test-Path frontend/.env.local)) { Copy-Item frontend/.env.example frontend/.env.local }
cd backend
# 仅尚无虚拟环境时创建
if (!(Test-Path .venv/Scripts/python.exe)) { py -3.13 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-db.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-rag.txt
cd ../frontend
npm ci
cd ..
```

执行任何后续写入步骤前，审阅目标库与以下副作用：

| 入口 | 写入范围与约束 |
| --- | --- |
| 手动 `CREATE DATABASE` / `GRANT` | 创建独立演示库、授予本地应用账号权限；保留现有库，不删除或清空数据 |
| `alembic upgrade head` | 创建/修改目标库的业务表、约束和 `alembic_version`；`0011` 会删除旧 `unanswered_questions` 表，故本基线只在独立新库执行 |
| `scripts.init_demo_users` | 仅 `users`，新增 maya/chen；冲突回滚，不重置已有密码 |
| `sql/test.sql` | `users`、`login_sessions`、`products`、`product_skus`、`orders`、`order_items`、`shipments`、`tracking_events`、`after_sale_requests`、`policies`、`faq`、`conversations`、`messages`、`tickets`、`low_confidence_questions`；固定键重复不覆盖，不是恢复初始状态 |
| `scripts.init_staff_user` | 仅 `users`，新增员工；邮箱冲突拒绝，不重置已有账号 |
| `import-markdown` / 可选 `import-faq` | `knowledge_chunks`（含来源状态和前后块关系）；更新来源可能使旧块 superseded 并清理旧向量 |
| `vectorize` | 写入/创建指定 Milvus 集合，并回填 MySQL `knowledge_chunks.vector_id/vector_status`；扫描目标库所有 pending 块 |
| `start.ps1` | 启动 MySQL，**自动执行 `alembic upgrade head` 和 `init_demo_users`**，再启动 API/Vite；不会执行业务 SQL、创建员工或导入知识。可选 `-EnableConversationMining` 另启会写挖掘/知识表并调用模型的任务，本基线不启用 |

只有用户明确选择执行这些准备命令时才写入。API 模块导入不自动建表。不要把密码、API Key 或真实连接串写入示例、命令历史或提交。

#### 2. 独立数据库与演示账号

现有库可能已混入多版本中英文知识；只导入一个文件**不会撤回其他来源**。已有 `vectorized` 块不会因更换集合名自动重建，因此不得将现有库直接切到新集合并声称获得纯净基线。使用新库（示例 `aiden_demo`）与新集合，保留原库/原集合。若这些名称已存在且有数据，先只读审阅，选择另一个未使用名称，不清空重置。

本仓库 `deploy/compose.mysql.yml` 只部署 MySQL，不部署 Milvus、embedding、API 或前端。先编辑本地配置的占位密码，然后显式启动 MySQL：

```powershell
docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml up -d mysql
# 交互输入管理员密码；下方 SQL 由用户审阅后在此客户端手动执行
docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml exec mysql mysql -u root -p
```

```sql
-- 示例库名和应用账号；按本机实际账号调整，不删除现有 aiden 库。
CREATE DATABASE aiden_demo CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
GRANT ALL PRIVILEGES ON aiden_demo.* TO 'aiden'@'%';
```

在 MySQL 提示符执行 `exit;` 返回 PowerShell，再进行下面的配置与命令。

在未提交的 `backend/.env` 和 `deploy/.env.mysql` 中将 `DATABASE_URL` 指向该库（账号/密码/端口保持实际值且两文件一致），供手动命令和 `start.ps1` 使用。已存在的 MySQL 数据卷不会因改 `MYSQL_DATABASE` 自动创建新库，以上创建与授权仍须手动执行。设置聊天服务的 `OPENAI_API_KEY`、兼容地址和可用模型；DeepSeek 示例在线客服明确设 `OPENAI_THINKING_MODE=disabled`，不要留空照抄后触发 tools/reasoning_content 400。

```powershell
cd backend
# 仅从已审阅的本地 .env 读取，显式覆盖旧进程 DATABASE_URL；输出被赋值捕获，不回显连接串。
# Alembic 和 init_demo_users 不自动加载 .env；缺项时只返回不含凭据的错误并停止。
$env:DATABASE_URL = (& .\.venv\Scripts\python.exe -c "import sys; from dotenv import dotenv_values; url=(dotenv_values('.env', interpolate=False).get('DATABASE_URL') or '').strip(); sys.exit('Missing DATABASE_URL in backend/.env') if not url else None; print(url)")
if ($LASTEXITCODE -ne 0 -or !$env:DATABASE_URL) { throw 'Cannot load DATABASE_URL from backend/.env; no preparation writes should run.' }
.\.venv\Scripts\alembic.exe -c alembic.ini heads
# 应为 0011_remove_unanswered_questions (head)；以下开始写目标库
.\.venv\Scripts\alembic.exe -c alembic.ini upgrade head
.\.venv\Scripts\alembic.exe -c alembic.ini current
.\.venv\Scripts\python.exe -m scripts.init_demo_users
cd ..
# 将 SQL 文件复制到容器；不执行数据库写入
docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml cp sql/test.sql mysql:/tmp/aiden-demo.sql
# 明确目标库，交互输入应用账号密码；source 才会写入上表所列业务数据
docker compose --env-file deploy/.env.mysql -f deploy/compose.mysql.yml exec mysql mysql -u aiden -p --database=aiden_demo
```

在该客户端先执行 `SELECT DATABASE();`，确认 `aiden_demo` 后显式执行 `SOURCE /tmp/aiden-demo.sql;`。SQL 不再含 `USE aiden`，目标库由连接参数决定；脚本不创建/删除数据库。导入后可只读执行 `SELECT order_no, user_id, status FROM orders;` 和 `SELECT policy_key, version, is_active, effective_from, effective_until FROM policies;` 核对归属与有效期。

检查完成后执行 `exit;` 返回 PowerShell，之后才执行员工创建、知识导入等 shell 命令。

| remote 身份 | 账号/密码 | 数据归属 |
| --- | --- | --- |
| SQL 测试用户 | `sql.customer@aiden.test` / `aiden123` | ID `11111111-1111-4111-8111-000000000001`；`TEST-20260922-001` 耳机 269 元、已发货；`TEST-20260918-002` 双肩包 329 元、已签收，已有 pending 退货申请 |
| 林沐言、陈屿 | `maya@aiden.demo`、`chen@aiden.demo` / `aiden123` | 只有登录身份，本 SQL 不给这两个用户订单；不能套用 mock 的订单 |
| SQL 测试客服 | `sql.staff@aiden.test` | 仅用于预置会话/工单关联，没有密码，不能登录 |
| 实际演示员工 | 用户自行设置 | 显式创建后可使用 `/staff/rag`、售后和人工工作台；mock `staff@aiden.demo` 不可用 |

员工账号：在 `backend` 的运行环境设置 `AIDEN_STAFF_EMAIL`、`AIDEN_STAFF_NAME`、`AIDEN_STAFF_PASSWORD`（至少 12 字符，私下安全输入），再**显式**执行 `.\.venv\Scripts\python.exe -m scripts.init_staff_user`。不输出/记录密码；重复执行已有邮箱会拒绝。

SQL 的 `test_return_policy`、`test_shipping_insurance`、`demo_refund_policy` 均为**演示政策，不是正式商家承诺**，起始 `2026-01-01`、active、无结束时间。订单退款核验与售后页面从有效 `policies` 读取（active 且 `effective_from <= 当前时间 < effective_until`，结束为空时无上界），退款演示使用 `demo_refund_policy` 的质量问题/货不对板/商品与描述不符/发错货受理口径，再交员工审核；不保证运费、金额或到账天数。示例订单日期固定，随时间推移不保证满足七天无理由期限；审批也不等于外部支付到账。

#### 3. 服务配置与精确知识导入

本基线固定 **`BAAI/bge-small-zh-v1.5` / 512 维 / 最大序列 512 tokens**，与 `.env.example` 的 **BGE-M3 / 1024 / 8192** 分开。修改本地 `backend/.env`：

```dotenv
EMBEDDING_BASE_URL=http://127.0.0.1:8001/v1
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_DIMENSION=512
EMBEDDING_MAX_SEQ_TOKENS=512
MILVUS_URI=http://127.0.0.1:19531
MILVUS_KNOWLEDGE_COLLECTION=knowledge_demo_bge_small_512
RAG_KNOWLEDGE_DIR=knowledge
RAG_ONLINE_STRATEGY=hybrid_rerank
RAG_RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

`EMBEDDING_TOKENIZER_PATH` 若配置，必须指向 **bge-small** 的 `tokenizer.json`，不能指向 M3；留空采用有安全余量的字符估算。切块上限由序列长度压到 512，不可将 M3 向量或其他模型的 512 维向量混用。离线与在线共用同一模型、维度和归一化配置；reranker 在 API 进程通过 FlagEmbedding 加载，须有其权重（或将变量指向真实本地模型目录）。

`settings` 使用 `.env` 时不会覆盖已有进程变量。开始员工创建、建库或启动 API 前，检查并清除旧进程中的 `EMBEDDING_*`、`MILVUS_*`、`RAG_*`、`OPENAI_*` 覆盖值，或确保它们与已审阅的本地配置一致；不要打印密钥。`DATABASE_URL` 则必须按上面的捕获赋值步骤显式覆盖，在新 PowerShell 窗口重做该步骤后才能执行迁移/账户写命令。

Milvus 必须支持原生 BM25（2.5+）。本机既有部署是 `milvusdb/milvus:v2.5.0` 的 `milvus run standalone`，宿主 `19531 → 19530`、`9092 → 9091`，采用 **embedded etcd (`ETCD_USE_EMBED=true`) 与 local storage (`COMMON_STORAGETYPE=local`)**，非本仓库 Compose 管理；此方式不需外置 etcd/MinIO。其他机器应先按 Milvus 官方 standalone 部署方式提供完整服务；若使用外置 etcd/MinIO，则这些依赖也须由该部署提供，不能只启动一个缺依赖的 Milvus 容器。本任务不新增 Compose/readiness 模块。

先只读核实已有 embedding `/health` 的模型和维度、Milvus `/healthz` 与实际集合 schema。本机可用地址：

```powershell
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:9092/healthz
```

若没有 embedding 服务，在单独窗口从 `backend` 显式启动现有脚本（首次加载可能下载模型）；不要在已有监听端口上再启动：

```powershell
.\.venv\Scripts\python.exe -m scripts.embedding_server --model BAAI/bge-small-zh-v1.5 --port 8001 --device cpu --max-length 512
```

**唯一 Markdown 导入清单：`商品FAQ.md`**。整个文件都是演示知识，包括保修/换码等示例口径，不能当正式业务授权；正式业务办理依据须由商家核准的有效 `policies` 和员工审核确认。**不导入** `README.md`、`退货政策.md`、`售后手册.md`、`product-faq.md`、`product-specs.md`、`returns-policy.md`、`after-sales-manual.md`、`billing-shipping.md`、`member-benefits.md`；这里不解决它们的冲突，也不使用全量目录导入。

```powershell
cd backend
# 精确单文件 glob；现有 CLI 没有 --file
.\.venv\Scripts\python.exe -m scripts.build_knowledge_base import-markdown --directory ./knowledge --pattern '商品FAQ.md'
# 显式写新集合+回填目标库；只在独立演示库执行，不能共享旧库 vectorized 状态
.\.venv\Scripts\python.exe -m scripts.build_knowledge_base vectorize
# 以下只读核对状态；不能以块数量替代实际检索/回答验收
.\.venv\Scripts\python.exe -m scripts.build_knowledge_base stats
.\.venv\Scripts\python.exe -m scripts.build_knowledge_base scan-pending
cd ..
```

`import-faq` 是**独立可选阶段**：它导入目标库所有 active FAQ，不能只凭 SQL 里有两行就假定范围只有两行。先只读审阅 `SELECT id, question, answer FROM faq WHERE is_active=1;`，明确范围后才显式执行 `scripts.build_knowledge_base import-faq`，再 `vectorize`；主基线不依赖此阶段。员工控制台也可按单文档 `商品FAQ.md` 导入，不能点 `import-markdown-all`。如果已有 chunks 为 `vectorized`，切集合不自动复制它们，本步骤不是旧知识迁移方案。

#### 4. 进入 remote 与验收边界

完成以上显式准备后，在根目录运行 `.\start.ps1`（会再次执行上表中的迁移/普通演示账号初始化），访问 `http://127.0.0.1:5173`。普通用户选择 user，用 **SQL 测试用户**查本人订单和物流；员工用手动创建的 staff 登录查看建库/售后/人工。同站点 Cookie 会共享登录态，用户和员工并行演示应使用不同浏览器或独立隐私会话，不是两个普通标签页。

不用脚本时，在 `frontend/.env.local` 设置 `VITE_API_MODE=remote`、`VITE_API_BASE_URL=/api`，分别运行后端 `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload` 和前端 `npm run dev`；代理端口要与后端一致。API `/api/health` 成功不代表登录、模型、检索或支付链路成功。

**2026-09-30 前轮只读核验记录（非本轮业务验收）**：脚本 help、精确单文件发现/切块、Alembic 图 head、MySQL `SELECT 1`、已有迁移 `0011_remove_unanswered_questions`、Milvus `2.5.0` 健康与旧 `knowledge_bm25` 512 维、embedding `8001` bge-small/512、旧 API `8000` health 均只读核验。旧库当时有 135 块混杂来源；此前没有清空或迁移它们。本记录不代表本轮准备或业务验收，后续授权写入结果见下文。
 
#### 本轮用户授权真实验收记录
**2026-09-30 本轮真实验收**：写入与知识准备只针对独立 MySQL `aiden_demo`、Milvus `knowledge_demo_bge_small_512`；旧库业务/知识未清空，旧库既有 135 块未导入新集合。曾误连外来 API 端口尝试登录，不将该请求计入验收，也不推断其是否写入 session；所有下述验收状态均来自我启动并核对 PID 的隔离 API（`127.0.0.1:57580`）及 remote Vite（`127.0.0.1:54273`）。两者已在本轮结束时停止。凭据从本地环境读取使用，未输出密钥、Cookie、含密码连接串或员工明文密码到报告或跟踪文件。

**准备步骤与结果**：

1. 从本地 `backend/.env` 安全读取连接参数，将库名显式改为新库；从 `deploy/.env.mysql` 仅在管理操作中读取本地管理员凭据。只新建 `aiden_demo` 并授权应用用户，没有 `DROP`、`TRUNCATE` 或重置旧库。
2. 在新库运行 `alembic upgrade head`、`current`，结果为 `0011_remove_unanswered_questions`；运行 `scripts.init_demo_users` 新增 maya/chen 两个普通用户。
3. 按审阅的写入范围执行 `sql/test.sql`，涉及 `users`、`login_sessions`、`products`、`product_skus`、`orders`、`order_items`、`shipments`、`tracking_events`、`after_sale_requests`、`policies`、`faq`、`conversations`、`messages`、`tickets`、`low_confidence_questions`；脚本不清空既有行。首次执行遇到 MySQL 1364：`messages.citations` NOT NULL 且无 server default，事务失败回滚。已在 `sql/test.sql:160-175` 的四条手写消息 INSERT 明确加入 `JSON_ARRAY()`；模型 `Message.citations` 的 ORM 默认不适用于手工 SQL。修复后同库执行成功，预置 `messages=4`。另显式新增一名随机临时员工，只用于本次真实员工验收；明文凭据未写入文档、日志或跟踪文件，用户复现仍按本页自行设置员工账号。
4. 唯一 Markdown 来源按精确参数 `import-markdown --directory ./knowledge --pattern '商品FAQ.md'` 导入 10 个 chunks；设置 bge-small `512` 维、最大序列 `512`，使用本机 embedding `8001`。`vectorize` 实际写入 `10`，MySQL 状态 `vectorized=10`、`pending=0`。Milvus 集合为 `knowledge_demo_bge_small_512`，配置维度 `512`，reranker 为本地 `BAAI/bge-reranker-v2-m3` 权重目录；在线 `RAG_ONLINE_STRATEGY=hybrid_rerank`。没有执行全量目录或可选 `import-faq`。
5. 隔离 API 在动态空闲端口 `57580`、Vite 在 `54273` 提供 remote；API health `200`、OpenAPI 包含 service 路由，Vite HTTP `200`。启动配置显式指定新库、新集合、embedding 模型/维度/max-length 与 `OPENAI_THINKING_MODE=disabled`；不修改本地 `.env`。

**实际 remote 代表场景（非完整 P0 全矩阵）**：

- SQL 用户登录 `200`；本人订单接口列出 `TEST-20260922-001`（已发货）和 `TEST-20260918-002`（已签收），本人售后列出预置 `TEST-AS-20260921-001`、`pending` 退货申请。真实模型 SSE 查询首单物流，返回顺丰运单 `SFTEST20260922001`、揽收及杭州转运中心轨迹。
- 真实模型 SSE 问“如何正确清洁运动鞋？”命中唯一 `商品FAQ.md` 块并引用有效正文；source URL `/api/knowledge/source/a52ac4f5-da30-4092-bd0a-e5575b43b57b` 经本人 Cookie 访问 `200`，返回文档原文。实际配置 `hybrid_rerank`，助手消息持久化 `retrieval_status=searched`、rank 1 `score=0.9992120172209172`；`search_faq` 将该值来自 `hit.rerank_score`。这是已运行路径上的 rerank 分数证据；没有单独 reranker stage trace，分数不是校准正确率。
- 退款原始完整输入“订单 TEST-20260922-001 的 QuietPods 耳机存在质量问题，我要申请退款，请先核对政策、说明需要的材料和提交边界。”曾两次失败：第一次原因摘录含商品主语导致全文匹配失败；第一次意图提示修订后仍输出“存在质量问题”，与政策正文“质量问题”不匹配。第二版 `backend/app/agent/intent.py:110-135` 明确排除无语义作用的肯定引导词并保留否定/假设/程度限定。相同完整输入在第二版后新会话真实 SSE 返回演示政策覆盖质量问题、所需照片/面单、员工审核及无到账时限承诺，并要求用户确认；按原话回复“确认提交”后真实创建 `ASA9B84F3B12D51A916BE81C74C17440FA`，本人售后接口确认订单 `TEST-20260922-001` 状态 `pending`。回复不承诺退款到账。首次失败记录仍保留在独立验收库；它没有被删除或覆盖。
- 已有 `TEST-20260918-002` 对话退款预览 `200` 返回有效 `demo_refund_policy` 演示政策原文；按确认提交因 seed 预置同商品的其他类型未结束退货申请，真实回复“该商品已有其他类型的未结束售后申请”，未重复创建申请。
- 否定输入“没有质量问题”及假设输入“如果……存在质量问题”均经真实 SSE 返回知识证据不足，均未产生肯定退款提交确认。此代表安全检查只证明未肯定创建；没有验收更广泛否定、假设或多诉求矩阵。
- 另一新会话真实转人工进入 `waiting`；临时员工登录 `200`、queue 可见、接单 `200` 进入 `staff`、发送员工回复 `200`、用户读取到员工消息、员工关闭 `200` 成为 `closed`；用户关闭后仍可读历史。
- `maya@aiden.demo` 真实登录 `200`；其本人订单与会话均为空；尝试读取 SQL 用户会话消息为 `404`。

本轮验收不覆盖完整 P0 第 3 节矩阵、全量 multi-intent / 并发员工竞争、Typora 预览或完整浏览器视觉交互。`intent.py` 的原因摘录契约是为严格政策全文匹配暴露的维护风险，需保留否定、假设、程度及时间限定，不能为通过而放宽 matcher；`sql/test.sql` citations 修复只显式补齐 ORM-only 默认字段。演示政策不是正式商家承诺；员工审核不代表外部支付退款已到账。

后端模型配置变量：

| 变量 | 用途 | 示例 |
| --- | --- | --- |
| `OPENAI_API_KEY` | OpenAI 兼容接口的密钥 | DeepSeek API Key |
| `OPENAI_BASE_URL` | 兼容接口地址 | `https://api.deepseek.com` |
| `OPENAI_MODEL` | 模型标识 | `deepseek-flash` |
| `SESSION_COOKIE_NAME` | 登录 Cookie 名称 | `aiden_session` |
| `SESSION_COOKIE_SECURE` | 是否只经 HTTPS 发送 Cookie | 本地开发使用 `false` |

未填写 `OPENAI_API_KEY` 或 `OPENAI_MODEL` 时，流式接口会返回明确的 SSE `error` 事件；不会用固定回答伪装成模型输出。订单、物流与知识检索依赖后端服务及数据状态，查询结果以实际工具响应为准。

每次模型调用最多加载最近 16 条消息，单条最多 2,000 字符，并将总历史上下文限制在 12,000 字符；模型回答上限为 800 tokens。

普通用户对话 API、转人工 `/handoff` 与员工工作台 `/api/staff/*`、员工建库 `/api/rag/*` 都已有后端实现。用户侧 `POST /api/conversations/{id}/handoff` 由 `backend/app/api/routes/conversations.py` 中的 `handoff()` 转交 `request_handoff()`，把会话从 `bot` 转入 `waiting` 队列；员工侧 `backend/app/api/routes/staff.py` 提供队列、接管、回复与结单接口。remote 模式会真实调用它们：`frontend/src/api/remote.ts` 的 `requestHandoff()`、`listQueue()`、`acceptConversation()`、`sendStaffMessage()`、`closeConversation()` 打到上述路由，`frontend/src/pages/StaffWorkspace.tsx` 每 3 秒轮询一次队列。转人工仍然是队列与会话状态，没有实时推送。

前端使用下列 JSON 形状作为契约。后端字段如需不同，可只在 `src/api/remote.ts` 做映射，不需要改页面。

- `POST /api/auth/login`：请求 `{ "account": "...", "password": "...", "role": "user" | "staff" }`，成功返回 Actor JSON：`{ "id": "...", "name": "...", "role": "user" | "staff", "email": "..." }`。登录页在 remote 模式选择角色，账号凭证由 FastAPI 校验。
- `GET /api/auth/me`：返回同样的 Actor；未登录返回 HTTP 401。401 被视为未登录，其它状态作为真实错误展示。
- `POST /api/auth/logout`：清理 Cookie，可返回 204。
- `GET /api/conversations`：返回会话数组或 `{ "items": [...] }`；`POST /api/conversations`：接受 `{}` 并返回新建 Conversation。
- `GET /api/conversations/{id}/messages`：返回消息数组或 `{ "items": [...] }`。
- `POST /api/conversations/{id}/messages/stream`：接受 `{ "text": "...", "clientMessageId": "..." }`，返回 `Content-Type: text/event-stream`。客户端携带 Cookie，并增量处理事件。
- `POST /api/conversations/{id}/handoff`：接受 `{}`，返回更新后的 Conversation。
- `GET /api/staff/queue`：返回待接入和服务中会话数组或 `{ "items": [...] }`。客服页面每 3 秒刷新队列。
- `POST /api/staff/conversations/{id}/accept`：接受 `{}`，返回已接入的 Conversation。
- `POST /api/staff/conversations/{id}/messages`：接受 `{ "text": "..." }`，返回已创建 Message。
- `POST /api/staff/conversations/{id}/close`：接受 `{}`，返回已结束的 Conversation。

员工建库接口均要求登录 Cookie 中的员工身份；mock 模式不会调用。`GET /api/rag/overview` 返回服务健康、知识块/批次/候选状态和文档列表；`GET /api/rag/milvus` 只读查看真实 Milvus 集合的维度、记录统计数和分页标量字段，并按主键核对 MySQL 状态；`GET /api/rag/preview?file=...` 只读预览知识目录中的 Markdown 切块；`GET /api/rag/chunks` 与 `GET /api/rag/mining` 查看入库块和挖掘摘要；`GET /api/rag/jobs` 查看当前 API 进程内的任务。`POST /api/rag/jobs/{kind}` 支持 `import-markdown`、`import-markdown-all`、`import-faq`、`mine`、`vectorize`、`cleanup`，其中单文档导入的请求体是 `{ "file": "知识目录内相对路径.md" }`。`POST /api/rag/embedding/start` 和 `/stop` 控制当前 API 进程亲自启动的本地 BGE 服务。写任务返回 202 表示已排队，冲突返回 409；任务结果从 `/jobs` 查询。`mine` 只运行一轮，不自动补向量，需另触发 `vectorize`。详见 [`docs/RAG.md`](../docs/RAG.md#员工建库控制台)。

`GET /api/rag/evals/customer-rag-v1` 也要求员工登录，只读返回 `backend/evals/reports/customer_rag_v1.json`，包含 15 道题 × 4 策略的总体、分桶和逐题结果。`/staff/evals` 通过 `ragApi.evalReport` 读取该接口；在页面点击“刷新报告”只重新读取文件。完整评估与标注重算命令见 [`docs/RAG.md` 的评估体系](../docs/RAG.md#评估体系)。

Conversation 建议包含 `id`、`userId`、`userName`（队列展示用，可选）、`subject`、`status`（`bot | waiting | staff | closed`）、`createdAt`、`updatedAt`、`assignedStaffId`、`assignedStaffName`、`lastMessagePreview`。时间用 ISO 8601 字符串。Message 包含 `id`、`conversationId`、`role`（`user | assistant | staff | system`）、`content`、`createdAt`、`status`（`complete | streaming | error | stopped`），订单回答可提供 `orderCard` 与 `toolStatuses`。`orderCard` 包含 `orderId`、`product`、`amount`、`status`（`物流中 | 已签收 | 退款中`）、`logistics`，以及可选的 `carrier`、`updatedAt`。

remote SSE 的阶段事件是 `progress`，data 必须包含 `stage` 与中文 `message`；stage 只能是 `recognition | query | retrieval | validation`。事件只表示真实执行：查询阶段来自实际工具启动（缓存命中而跳过的工具不虚报），检索/校验阶段对应实际证据评估与最终引用检查。进度仅显示在当前会话的瞬态提示中，不写入消息正文或工具结果。

远端正常流顺序为 `start → (progress | delta)* → final → handoff? → done`；`start` 给出数据库助手消息 ID，`delta` 是真实生成阶段的增量片段，前端追加为“生成草稿 · 尚未核验”。`final` 携带核验且落库后的完整权威正文及引用数组，替换草稿和引用（空数组也清空旧引用），不是终止事件；`done` 标记本轮完成。`error` 是独立终态，不能与 `done` 连发。remote 不发送独立 `citations` 事件，也不展示内部意图、reasoning 或工具结果为回答正文。

remote 历史轮询合并保留服务端返回的消息顺序，尚未出现在历史中的本地消息按缓存顺序尾附。流式助手草稿沿用客户端时间，而落库消息使用服务端 UTC 时间（微秒精度）；不再混用 `createdAt` 字符串重排，以免时钟差或小数精度差使回复在生成中跳到用户消息上方。`start` 改 ID、`final` 替换正文及轮询同步均不改变本轮消息先后，mock 契约不变。

```text
event: start
data: {"messageId":"assistant-message-id"}

event: progress
data: {"stage":"recognition","message":"正在识别本轮诉求…"}

event: delta
data: {"text":"正在生成的"}

event: delta
data: {"text":"临时草稿。"}

event: progress
data: {"stage":"validation","message":"正在检查证据与引用…"}

event: final
data: {"text":"已经完成校验并保存的整轮回答。","citations":[]}

event: done
data: {}

```

异常流以独立 `error` 终态结束，不与正常流的 `done` 连发，例如：

```text
event: start
data: {"messageId":"assistant-message-id"}

event: error
data: {"error":"订单服务暂时不可用"}
```

mock 不模拟上述真实阶段与权威 `final`：它保留本地演示的 `tool_status`、`order_card` 和分段 `delta` 语义，仍由本地 `done` 完成回答。它们不代表远端工具进度或经过后端证据校验的输出，remote 失败不会回退 mock。

草稿可能含不可靠断言或非法引用，证据检查失败时 `final` 可用拒答完整替换；已经显示给用户的草稿无法撤回，应以最终核验回答为准。用户点击“停止生成”实际 abort 当前 remote 请求；断线或无终态 EOF 也按未完成处理。未保存的草稿不落库，服务端仅将尚未完成的 `streaming` 助手消息记为 `stopped`，已落库的 `complete` 不降级。页面停止/错误时保留瞬态草稿说明，流结束后同步数据库，历史可能因此恢复空正文 `stopped` 或服务端错误正文。手动重试复用原文和 `clientMessageId`，清空本轮草稿、引用与进度；服务端幂等配对不重复插入用户消息。已完成同 key 请求重放 `start → final → done`，不再执行模型或发送阶段进度；历史恢复 `complete` 后清除重试入口。不会自动重试。

remote 历史仍每 3 秒轮询全部消息，会话状态每 5 秒刷新，保留人工留言与接管/结单同步。当前活动流的 `streaming` 历史不能抹去草稿，`final` 或数据库 `complete` 先到后都禁止迟到增量再追加或状态降级；两者任意顺序只显示一条权威助手回答并保留引用。结束后重新读取数据库。会话切换会取消旧请求；请求身份同时隔离旧回调和旧 `finally`，防止其修改新会话或同会话的新草稿。

解析器支持 UTF-8 分片、LF/CRLF/CR 行结束、多行 data、注释行、分片中的未完成行，以及流结束时最后一个没有空行终止的事件。转人工后用户仍可在原会话继续留言：用户侧 `POST /api/conversations/{id}/messages` 在 `waiting`/`staff` 阶段写入留言（`backend/app/api/routes/conversations.py` 中的 `send_user_human_message()`），用户页输入框保持可用，`frontend/src/pages/UserWorkspace.tsx` 的 `send()` 在这两个状态下改调 `api.sendHumanMessage`。

## 代码组织

- `src/pages/`：登录、用户会话、mock/remote 两模式人工客服工作台、`RagConsole.tsx` 员工建库控制台、`RagEvals.tsx` 评估页面及 `Observability.tsx` 只读观测页。
- `src/components/`：品牌、模式提示、会话状态等共享 UI。
- `src/api/contracts.ts`：页面使用的统一适配器接口。
- `src/api/mock.ts`：本地持久化、跨标签同步和模拟 Agent 行为。
- `src/data/demoData.ts`：演示账号和各用户独立的订单数据。
- `src/api/remote.ts`：Cookie、HTTP 和增量 SSE 解析。
- `src/api/rag.ts`：员工建库与评估报告 API、Cookie 请求和错误处理。
- `src/api/observability.ts`：员工只读观测 API、Cookie 请求及明确错误处理。
- `src/types.ts`：身份、会话、消息、订单、流事件与观测 API 类型。

TanStack Query 管理服务端数据及刷新；不使用额外全局状态库。普通用户与客服使用独立路由，最终权限仍由 API 服务端校验。

## 员工旁路主题分类

入口为 `/staff/topics`，员工顶栏和账户下拉菜单中的“主题分类器”均可到达。`App.tsx` 使用 `RoleGate role="staff"`，`Common.tsx` 显示专用工作区名称；普通用户没有这两个入口，直接访问会重定向到用户工作区。最终权限由后端员工依赖校验，前端角色门不是 API 授权替代品。

调用链：`TopicConsole.tsx` → TanStack Query → `src/api/topics.ts` → 带 Cookie 的 `/api/topics`。使用现有 `VITE_API_MODE=remote`、`VITE_API_BASE_URL` 和 Vite 的 `VITE_API_PROXY_TARGET`；mock 模式禁用真实分类、统计读取和评测，不生成模拟结果，也不在远端失败时回退 mock。模型、设备、模型产物和数据集路径只由服务端部署配置决定，页面不接收路径或任意命令。

页面固定提示：**旁路主题分类用于低置信度问题分析，不影响在线客服回答、意图路由或工具授权。**

四个页签和接口：

- **运行概览**：`GET /availability` 展示服务端模型版本、taxonomy、数据库、模型加载和冻结数据检查及明确阻塞原因；`GET /jobs`、`GET /jobs/{id}` 读取真实任务历史/详情。`POST /jobs/batch` 只提交 `start_at/end_at/limit/batch_size/after`，总量 1–5000、模型批大小 1–500。日期按浏览器本地日历日（包含首尾日期）转换为 UTC 半开区间。实际返回任务 ID 后才显示任务；只对 queued/running 轮询，刷新页面从服务端重新恢复历史，不显示估算百分比。实际批次结果提供 `processed/persisted/stale_source/next_cursor` 时才显示计数与续跑；不自行制造游标。活动任务禁用重复启动，服务端 409 保留清晰冲突错误。
- **分类结果**：`GET /results` 分页，每页 20 条，按时间、单一模型版本、主题、分类状态、是否有当前预测和是否有当前人工标注筛选。`GET /results/{source_id}` 展示主题脱敏原话、来源时间/ID、独立模型预测和人工最终标注、全部标签 sigmoid 分数、模型/taxonomy/输入哈希/日期/当前标记。sigmoid 分数不是校准后的正确概率。无当前预测的“未分类”、模型“不确定”、人工“上下文不足”和业务标签“其他”严格分开；过期预测不计作当前预测。会话链接沿用 `/staff/{conversationId}`；审核链接 `/staff/reviews?review_id=...` 由 ReviewQueue 打开对应只读/审核详情，关闭时清除参数。此主题页面不编辑标注。
- **主题统计**：`GET /stats` 必须有真实单一模型版本，仅应用时间与模型筛选，不应用结果页标签/状态/当前预测/人工筛选。服务端 current 输入与当前 taxonomy 的模型统计、独立人工统计、原始来源数和归并审核去重数分开展示；多标签数之和可超过来源数。没有模型或读取失败时显示阻塞，不以零补缺失值。
- **评测报告**：`GET /reports`、`GET /reports/{id}` 读取真实报告，历史版本独立查看。`POST /jobs/evaluation` 仅运行服务端绑定的真实编码器及原冻结、人工确认、隐私审核通过的测试集，没有 LLM 评审或任意数据集路径入口。报告展示数据集和划分哈希、模型/taxonomy、生成时间、micro F1、有支持类别 macro F1、逐类 support/precision/recall/F1、覆盖/多标签、混淆与修订需求、安全错误样例、耗时和吞吐。null 显示“未评估 / 不适用”，无支持类别标为证据不足。质量、覆盖、性能与阈值结论来自服务端确定性结论，不发明阈值；历史报告不能证明当前模型质量或生产就绪。没有真实报告时显示“尚无法形成分类质量结论”及所缺前提。

### 首轮界面验证（历史，数据库重启前）

首轮 `npm run build`（Windows `npm.cmd`）通过；Vite 提示现有主包超过 500 kB，未运行无关测试套件。实际 Chromium 打开本地 Vite 页面并通过代理访问真实 FastAPI 主题路由，观察了四页签、筛选请求、禁用启动、空任务历史刷新恢复、无模型统计阻塞与无报告结论。员工顶栏/账户菜单入口存在；受控普通用户访问主题页重定向 `/app`，无员工主题入口，主题 API 返回 403；匿名主题 API 返回 401。

**首轮身份验证范围限制**：数据库不可连接，因此那一轮只使用临时服务的身份依赖替代来做界面与员工权限边界 smoke；主题路由、员工授权依赖、模型/数据库/数据集 readiness 均为真实实现，没有 API 路由 mock，也没有模拟分类成功。真实账号登录、数据库会话恢复未验收。临时身份机制不进入产品代码或部署配置。本轮（2026-10-08）实际账号验收见下方最新记录。

当前检查返回 `database_unavailable`、`artifact_unconfigured`、`dataset_unconfigured`、`frozen_binding_unavailable`，`can_classify/can_evaluate` 均为 false；taxonomy 检查通过。后端只读诊断为配置数据库端口 3307 连接失败（2003），现有 3308 监听使用相同凭证只读探测被拒绝（1045）；没有修改配置、启动数据库或迁移。**这不是“历史缺表/缺列”结论**，连通后才能核验当前表结构；页面按实际服务端缺表/缺列字段展示结构阻塞。

真实模型、绑定且经人工/隐私审核的冻结数据集、可用数据库与真实员工会话仍须部署后验收。当前没有任务或真实报告，无法实际证明成功分类、分页有数据的结果/统计、活动任务跨刷新、续跑游标、409并发冲突、报告指标及错误样例视觉内容；未以 fixture 或空报告伪造这些证明，更未接受分类质量或生产就绪结论。

**2026-10-08 本轮重新验收**：后端 worker 启动的 current-code/no-overrides API 为 `http://127.0.0.1:54110`，remote Vite 为 `http://127.0.0.1:49832`，均使用临时独占验收端口；验收完成后后端 worker 已停止自己启动的 API/Vite 进程并清理其日志。实际 Chromium 打开登录页，使用文档中的普通用户 `maya@aiden.demo` 完成真实登录；页面进入 `/app/<conversation-id>`，显示 remote 模式和真实会话历史，未将密码、Cookie 或 session 值写入记录。作为该用户访问 `/staff/topics` 后实际重定向回 `/app/<conversation-id>`，顶部没有“主题分类器”入口；同一已登录浏览器对 `GET /api/topics/availability` 得到 `403`（“仅员工可访问此功能”）。匿名 `GET /api/topics/availability` 与 `GET /api/auth/me` 实际返回 `401`（“请先登录”）。这些观察验证了普通用户的真实前端角色门和服务端权限边界、匿名 API 鉴权及本轮用户登录路径；未验证员工登录、主题页员工导航/四页签或实际结果/详情/筛选/统计/报告界面，也未做截图存档。

本轮浏览器验收当时后端只读核验为数据库schema `0011`、主题所需三张表缺失、6条来源记录；未配置模型/冻结数据集，也没有主题版本、任务或报告。真实员工凭据不可取得，故未伪造Cookie、身份或员工登录；未运行分类/评测任务。本轮未改源码、配置或依赖，未运行构建/测试，前端README是该轮唯一修改文件。

**2026-10-08 后续授权迁移（最新数据库状态）**：用户明确授权仅执行现有 `0012_topic_classification`，当前.env目标 `127.0.0.1:3307/aiden` 已实际从0011升级到0012（Alembic exit 0）。三张主题表及消费者列就绪，真实 `repo.readiness()` 和availability的database检查均ready；三个新表实际行数均0，原业务表行数保持来源6、消息206、审核队列1、用户5。真实结果读取六条均未分类，topic脱敏/hash检查6/6通过。仍无已配置模型或冻结数据，`can_classify/can_evaluate=false`、版本/任务/报告为空；未用虚构版本跑统计，员工主题页成功访问与实际分类/评测仍未验收。本次只执行迁移及直接消费者smoke，没有重启API/浏览器或修改页面。完整范围、约束与未满足前提见[主题分类器授权0012迁移验收](../docs/主题分类器.md#2026-10-08-授权0012迁移验收)；此前缺表记录仅代表历史时点。
