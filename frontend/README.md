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

## 连接 FastAPI

FastAPI 提供普通用户对话闭环，以及员工专用 RAG 建库控制台和评估页面。remote 员工登录后进入 `/staff/rag`，可预览切块、导入知识、挖掘对话和补齐向量；从这里进入 `/staff/evals`，可查看四策略指标、按题型分桶结果，并按题型和难度展开核对每道题。评估页可手动启动完整评估，后台运行时显示进度，完成后自动读取新报告；“刷新报告”只重新读取已保存结果。人工客服接管会话的 `/api/staff/*` 接口仍未实现，remote 员工不会进入 mock 的接待工作台。订单和物流工具是否返回数据取决于后端数据与服务配置。

remote 用户页中，知识类回答的 `[1]` 等引用编号可点击查看来源 chunk 的章节路径和正文；Markdown 来源提供跳回原文的链接。每段已完成的助手回答下方有“满意 / 不满意”反馈，选择后显示“已反馈”并锁定。反馈当前只按用户和消息保存在浏览器 `localStorage`，不上传后端；浏览器换设备或清理本地数据后不会同步。

设置：

```dotenv
VITE_API_MODE=remote
VITE_API_BASE_URL=/api
```

开发服务器默认将 `/api` 代理到 `http://127.0.0.1:8000`。使用根目录 `start.ps1` 时，可在启动前设置 `FASTAPI_PORT`，脚本会用该端口启动后端，并让 Vite 代理自动指向同一端口：

```powershell
$env:FASTAPI_PORT = '8001'
.\start.ps1
```

单独运行前后端时，后端的 Uvicorn 端口和前端启动环境中的 `VITE_API_PROXY_TARGET` 要保持一致，例如将后者设为 `http://127.0.0.1:8001`。remote 请求全部使用 `credentials: 'include'`，登录态只从服务端 HttpOnly Cookie 恢复。若改为跨域直连，服务端还需要允许对应前端来源并启用凭证 CORS。remote 接口报错会直接呈现给用户，不会切回 mock。

### 本地启动

需要 Python 3.10+、Node.js、npm、MySQL。真实建库和在线混合检索还需要按 [`docs/RAG.md`](../docs/RAG.md) 配置与集合维度一致的 embedding 服务、Milvus 2.5+、`bge-reranker-v2-m3` 及对话模型；历史对话挖掘另需配置抽取模型。在仓库根目录开两个 PowerShell 窗口。

后端窗口：

```powershell
cd backend
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r requirements-rag.txt
Copy-Item .env.example .env
```

编辑 `backend/.env`，填入 DeepSeek API Key。示例已设置 `OPENAI_BASE_URL=https://api.deepseek.com` 与 `OPENAI_MODEL=deepseek-flash`；如果你的 OpenAI 兼容服务使用不同地址或模型标识，可在这里调整。然后启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

首次启动前，在 `backend/.env` 中配置 MySQL `DATABASE_URL`，然后从 `backend` 目录运行 `.\.venv\Scripts\alembic.exe -c alembic.ini upgrade head` 和 `.\.venv\Scripts\python.exe -m scripts.init_demo_users`。数据库表由 Alembic 管理，演示账号需显式初始化。建库控制台员工账号另由 `.\.venv\Scripts\python.exe -m scripts.init_staff_user` **人工执行**创建：先在运行环境设置 `AIDEN_STAFF_EMAIL`、`AIDEN_STAFF_NAME`、`AIDEN_STAFF_PASSWORD`（至少 12 字符），脚本不会覆盖已有账号或密码。不要把真实密码或 API Key 放入示例文件或提交到仓库。

前端窗口：

```powershell
cd frontend
Copy-Item .env.example .env.local
```

将 `frontend/.env.local` 改为：

```dotenv
VITE_API_MODE=remote
VITE_API_BASE_URL=/api
```

然后启动：

```powershell
npm run dev
```

访问 Vite 显示的地址。普通用户演示账号为 `maya@aiden.demo` 或 `chen@aiden.demo`，密码均为 `aiden123`。选择员工身份并使用上一步创建的员工账号登录后进入 `/staff/rag`。mock 的 `staff@aiden.demo` 只属于浏览器演示数据，不能用于真实建库。`start.ps1` 默认不启动定时对话挖掘；显式使用 `.\start.ps1 -EnableConversationMining` 才启动独立的周期任务。

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

普通用户对话 API 与员工建库 `/api/rag/*` 已有后端实现。下方契约中 `/handoff` 与 `/staff/*` 是 mock 接待工作台使用的预留接口，当前后端没有实现；remote 模式不会发起转人工请求。

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

SSE 至少支持以下事件。每个事件一行 `event:`，数据放在一行或多行 `data:` 中；多行 data 会按 SSE 规则拼接后解析。也可以在 JSON 中用 `type` 指定事件类型。

```text
event: start
data: {"messageId":"assistant-message-id"}

event: tool_status
data: {"status":"正在查询订单…"}

event: order_card
data: {"order":{"orderId":"AD-...","product":"商品","amount":269,"status":"物流中","logistics":"运输中"}}

event: delta
data: {"text":"订单正在"}

event: handoff
data: {}

event: done
data: {}

event: error
data: {"error":"订单服务暂时不可用"}
```

解析器支持 UTF-8 分片、LF/CRLF/CR 行结束、多行 data、注释行、分片中的未完成行，以及流结束时最后一个没有空行终止的事件。建议服务端在客户端断开流时也将助手消息状态落为 `stopped`，以便刷新后保持一致。当前给定契约没有“转人工后用户追加消息”的独立 endpoint，因此转接后用户页展示状态和客服回复，输入框切换为只读。

## 代码组织

- `src/pages/`：登录、用户会话、mock 人工客服工作台、`RagConsole.tsx` 员工建库控制台及 `RagEvals.tsx` 评估页面。
- `src/components/`：品牌、模式提示、会话状态等共享 UI。
- `src/api/contracts.ts`：页面使用的统一适配器接口。
- `src/api/mock.ts`：本地持久化、跨标签同步和模拟 Agent 行为。
- `src/data/demoData.ts`：演示账号和各用户独立的订单数据。
- `src/api/remote.ts`：Cookie、HTTP 和增量 SSE 解析。
- `src/api/rag.ts`：员工建库与评估报告 API、Cookie 请求和错误处理。
- `src/types.ts`：身份、会话、消息、订单与流事件类型。

TanStack Query 管理服务端数据及刷新；不使用额外全局状态库。普通用户与客服使用独立路由，最终权限仍由 API 服务端校验。
