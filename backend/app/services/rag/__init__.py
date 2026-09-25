"""离线建库（RAG）模块。

子模块分工：

* `length`：token 计量与句子边界，切分长度判断的唯一来源；
* `chunking`：结构感知切分，负责“不切半句、超长章节递归、表格按行分块”；
* `knowledge`：Markdown 章节解析与知识块构建（含 FAQ 行）；
* `embedding_text`：向量化文本的唯一拼接格式；
* `embedding`：BGE-M3 dense 向量客户端；
* `indexing`：建库编排（MySQL -> BGE-M3 -> Milvus -> 回填状态）。

本模块只服务于离线建库，在线客服工具（`app.services.tools.customer`）当前不读取它。
"""
