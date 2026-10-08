# Vibe 合成主题分类人工复核快照

这是供人工检查的只读导出快照，不是最终训练/验证/测试数据集，也不是人工确认结果。所有条目均为合成文本；`reference_labels` / `reference_topics` 是生成时的预标注，仅作待复核参考，不代表真值。

## 文件

- `review_worksheet.csv`：可在 Excel 中查看；UTF-8 BOM。每行是一个稳定 `sample_id`，包含原文、预标注、冻结前 `original_planned_split` 和自动盲审状态。
- `review_questions.jsonl`：同一监督候选的机器可读快照。
- `challenge_review_worksheet.csv` / `challenge_questions.jsonl`：独立的无明确主题/信息不足挑战文本；参考标签留空，不计入监督候选。
- `excluded_scenarios.json`：按整组排除的场景和理由；不会混入监督工作表。
- `manifest.json`：输入与快照指纹、数量、文件哈希和状态。

## 如何复核

请先复制 `review_worksheet.csv`（如需挑战题，再复制对应挑战 CSV）到单独的人工复核目录；不要改动本只读快照、原始批次或冻结场景计划。仅填写空白的 `reviewer`、`review_status`、`reviewed_labels`、`review_note` 列；标签仍使用英文 taxonomy ID，按原 taxonomy 顺序填写。`reference_labels` 与 `reference_topics` 不要当作真值，逐条对照 `text` 独立判断。对挑战题可按实际要求填状态/说明，但不得把空参考标签解释为已确认无主题。

`auto_review_status` 仅表示当前自动化跨工作者文本盲审记录：`passed`=标签、字面证据、状态及自然度门槛一致；`disputed`=标签/证据/自然度有分歧或质量标记；`pending`=无可用有效跨工作者判断。它不是人工复核。`auto_review_flags` 用于提示争议/文本质量，不替代逐条判断。

`original_planned_split` 是场景在表达生成前冻结计划中的原始组划分，不是最终重去重后的切分，也不代表已进入最终数据集。完整装配、跨组去重、筛选和人工隐私/语义确认尚需后续流程；本快照不能用于训练或评估。

所有行均保留 `human_reviewed=false`；人工复核字段为空。当前数据集仍待人工隐私确认与语义确认。
