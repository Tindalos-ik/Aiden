# 知识文档目录

本目录存放离线建库（RAG）要导入的 Markdown 知识文档：退货政策、商品 FAQ、售后手册等。

- 目录位置可通过环境变量 `RAG_KNOWLEDGE_DIR` 修改，相对路径按 `backend/` 解析。
- 导入命令：在 `backend/` 下执行 `python -m scripts.build_knowledge_base import-markdown`。
- 切分按 Markdown 标题层级进行，因此文档应使用规范的 `#` / `##` / `###` 标题组织章节：
  章节标题会作为该块没有天然问题时的问法（`questions`），上级标题路径会成为 `category`。
- 大表格建议使用标准 Markdown 表格语法（表头 + 分隔行），导入时按行分块并逐块复制表头。
- 当前目录中的三份文档是用于验证切分与建库链路的示例内容，不代表真实商城政策。
