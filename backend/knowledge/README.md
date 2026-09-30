# 知识文档目录

本目录存放离线建库（RAG）的演示 Markdown，不是可直接全量导入的正式业务政策库。可复现 remote 准备步骤统一见 [`frontend/README.md` 的「本地启动」](../../frontend/README.md#本地启动)。

- 本轮演示唯一 Markdown 清单：**`商品FAQ.md`**，全文件都是示例，包括保修/换码口径；业务授权须由商家核准的有效 `policies` 与员工审核确认。
- **不导入**：`README.md`、`退货政策.md`、`售后手册.md`、`product-faq.md`、`product-specs.md`、`returns-policy.md`、`after-sales-manual.md`、`billing-shipping.md`、`member-benefits.md`。中英文及不同主题版本未在本轮统一，不用全量导入掩盖冲突。
- 主路径：在 `backend/` 显式执行 `python -m scripts.build_knowledge_base import-markdown --directory ./knowledge --pattern '商品FAQ.md'`；现有 CLI 没有 `--file`，这里使用精确单文件 glob。员工单文档入口也可指定 `商品FAQ.md`，不要执行 `import-markdown-all`。
- FAQ 表导入为独立可选阶段，`import-faq` 会导入所有 active 行，须先只读审阅范围。Markdown 不自动变成 `policies`。
- 基线为 bge-small-zh-v1.5 / 512 维 / max_seq_tokens=512，新集合 `knowledge_demo_bge_small_512`；示例 BGE-M3 / 1024 属于另一配置。新集合必须配独立新演示库；旧 MySQL 的 vectorized 状态不会因改集合名自动重建。
- 目录位置可通过环境变量 `RAG_KNOWLEDGE_DIR` 修改，相对路径按 `backend/` 解析。
- 切分按 Markdown 标题层级进行，因此文档应使用规范的 `#` / `##` / `###` 标题组织章节：
  章节标题会作为该块没有天然问题时的问法（`questions`），上级标题路径会成为 `category`。
- 大表格建议使用标准 Markdown 表格语法（表头 + 分隔行），导入时按行分块并逐块复制表头。
- 本目录中的政策、FAQ 和售后手册均为演示/建库示例，不代表已获授权的喵购商城正式政策；同目录中英文本可能存在退款时效、换货与运费责任冲突，不能仅因导入成功就将检索片段当作已生效条款。正式上线前须业务逐项核准并统一清理冲突版本。
- 检索到本目录内容只表示「库里有一条示例说明」，不构成提交退款、退货或换货的授权，也不代表审核会通过；退款能否提交、能否通过取决于商户已生效的售后政策与人工审核结果。

## 外部平台做法参考（不在本目录）

京东、淘宝、天猫等外部平台关于「货不对板 / 商品与描述不符」的官方条款摘录、查阅日期和仍需业务决策的事项，统一存放在**索引目录之外**的 `docs/售后政策外部参考.md`。

默认目录导入按 `**/*.md` 匹配（`app/services/rag/indexing.py::discover_markdown_files`），连本 README 也会入库；这就是基线必须指定精确清单而非执行默认全量命令的原因。不要把外部平台参考写进本目录：它们只描述其他平台做法，既不适用于喵购，也不构成退款授权。喵购要采纳原型建议，须先完成 `docs/售后政策外部参考.md` 列出的业务决策；确认之前只能受理并转人工审核。
