# 客服知识库四策略评估（v1）

本报告使用 15 道人工标注题：12 道可回答、3 道库外题。难度为 easy 4 道、medium 6 道、hard 5 道；可回答题的 ground truth 标注到来源文件和章节。逐题召回、答案、评审原因见 [原始报告](customer_rag_v1.json)。

运行环境：Milvus 2.5.0、`knowledge_bm25` 集合中的 84 条有效 chunk；dense 模型为 512 维 `bge-small-zh-v1.5`，重排模型为本地 CPU 上的 `bge-reranker-v2-m3`，Faithfulness 评审模型为 `deepseek-flash`。这里没有测试 1024 维 BGE-M3 dense 模型。四策略共运行 60 次检索与答案评审。

## 总体对比

Recall 与 MRR 只统计 12 道可回答题；Recall@K 是目标章节在前 K 条中的平均覆盖率，多证据题按两个目标章节分别计数。Faithfulness 统计全部 15 道题。库外拒答率只统计 3 道库外题。

| 策略 | Recall@1 | Recall@5 | Recall@10 | MRR | Faithfulness | 库外拒答率 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 纯向量 | 0.5000 | 0.9167 | 1.0000 | 0.7292 | 1.0000 | 1.0000 |
| 纯 BM25 | 0.7083 | 1.0000 | 1.0000 | 0.8472 | 1.0000 | 1.0000 |
| 混合 RRF | 0.7500 | 1.0000 | 1.0000 | 0.9028 | 1.0000 | 1.0000 |
| 混合 + Rerank | 0.8333 | 1.0000 | 1.0000 | 0.9583 | 1.0000 | 1.0000 |

## 按问题类型分桶

各单元格为 `Recall@1 / MRR`；无答案题没有检索 ground truth，报告其拒答率。

| 类型（题数） | 纯向量 | 纯 BM25 | 混合 RRF | 混合 + Rerank |
| --- | ---: | ---: | ---: | ---: |
| 具体型号（4） | 0.0000 / 0.3125 | 1.0000 / 1.0000 | 0.5000 / 0.7083 | 1.0000 / 1.0000 |
| 口语问法（2） | 1.0000 / 1.0000 | 1.0000 / 1.0000 | 1.0000 / 1.0000 | 1.0000 / 1.0000 |
| 政策边界（4） | 0.7500 / 0.8750 | 0.5000 / 0.7083 | 1.0000 / 1.0000 | 0.7500 / 0.8750 |
| 多证据（2） | 0.5000 / 1.0000 | 0.2500 / 0.6667 | 0.5000 / 1.0000 | 0.5000 / 1.0000 |
| 库外拒答率（3） | 1.0000 | 1.0000 | 1.0000 | 1.0000 |

按难度的完整指标也在原始 JSON 的 `strategies.*.by_difficulty` 中。具体型号题中，BM25 四题的目标章节均排第 1，验证了型号字符串检索；重排后也均排第 1。

## 解读与限制

- 混合加重排的总体 Recall@1 和 MRR 最高；政策边界桶中，单纯混合优于重排。样本量小，不能把这些差异外推为线上胜率。
- Faithfulness 的 1.0000 表示模型评审认为答案中的事实有本次召回证据支持，不代表答案完整或业务事实正确。库外题的纯拒答也可得 1 分。
- 语料有跨来源冲突：`product-faq.md` 写未满 99 元运费为 10 元，`billing-shipping.md` 写为 6 元；本次 Faithfulness 不检测这种冲突。`退货政策.md` 也含退款时效参考范围，而生成提示禁止承诺具体到账时间，评审需结合逐题答案人工复核。
- 原始生成答案和 Faithfulness 评审保留在 JSON 中。三道政策题曾误标为未入库的 `returns-policy.md`；修正为实际已入库的 `退货政策.md` 后，只用保存的召回列表重算检索指标，没有重跑生成模型。重算命令见下文。

## 复现

在 `backend` 目录配置可访问的 MySQL、embedding 服务、Milvus 2.5+ 集合和模型后，运行完整评估：

```powershell
$env:MILVUS_URI = 'http://127.0.0.1:19531'
$env:RAG_RERANKER_MODEL = 'D:\hf-cache\bge-reranker-v2-m3'
$env:HF_HOME = 'D:\hf-cache'
.\.venv\Scripts\python.exe -m evals.run_customer_rag --collection knowledge_bm25 --report evals/reports/customer_rag_v1.json
```

仅在标注变化、问题正文及召回不变时，可从已有报告重算检索指标，并保留已有答案与评审：

```powershell
.\.venv\Scripts\python.exe -m evals.run_customer_rag --recompute-from evals/reports/customer_rag_v1.json --report evals/reports/customer_rag_v1.json
```
