"""四种在线检索策略的可复现评估；只读取知识库，不写业务表。

运行示例（backend 目录）：
    python -m evals.run_customer_rag --report evals/reports/customer_rag_v1.json

默认同时评 Recall@1/5/10、MRR 和生成答案的 Faithfulness。Faithfulness 使用当前配置的
对话模型逐条判断答案中的事实是否由本次召回证据支持；报告保留每道题、每种策略的原始判定。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.config.settings import settings
from app.services.rag.retrieval import RetrievalStrategy, semantic_search


STRATEGIES: tuple[RetrievalStrategy, ...] = ("dense", "bm25", "hybrid", "hybrid_rerank")
DATASET = Path(__file__).with_name("customer_rag_v1.jsonl")


def _model() -> ChatOpenAI:
    kwargs: dict[str, Any] = {"model": settings.openai_model, "api_key": settings.openai_api_key,
                              "temperature": 0, "max_tokens": 600}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    if settings.openai_thinking_body:
        kwargs["extra_body"] = settings.openai_thinking_body
    return ChatOpenAI(**kwargs)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") for part in value if isinstance(part, dict))
    return ""


def _json_object(value: str) -> dict[str, Any]:
    raw = value.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return result


def _retrieval_numbers(hits: list[Any], labels: list[dict[str, str]]) -> dict[str, float | None]:
    return _retrieval_numbers_from_rows(
        [{"source_path": hit.chunk.source_path, "section_path": hit.chunk.section_path} for hit in hits],
        labels,
    )


def _retrieval_numbers_from_rows(hits: list[dict[str, Any]],
                                 labels: list[dict[str, str]]) -> dict[str, float | None]:
    if not labels:
        return {"recall@1": None, "recall@5": None, "recall@10": None, "mrr": None}
    positions = [next((rank for rank, hit in enumerate(hits, 1)
                       if Path(hit.get("source_path") or "").name == label["source_path"]
                       and label["section"] in (hit.get("section_path") or "")), None)
                 for label in labels]
    first = min((position for position in positions if position is not None), default=None)
    return {
        f"recall@{k}": sum(position is not None and position <= k for position in positions) / len(labels)
        for k in (1, 5, 10)
    } | {"mrr": 1 / first if first else 0.0}


def _answer_and_faithfulness(model: ChatOpenAI, question: str, hits: list[Any]) -> tuple[str, float, str]:
    evidence = [
        {"citation": f"[{index}]", "section": hit.chunk.section_path,
         "content": hit.chunk.answer}
        for index, hit in enumerate(hits, 1)
    ]
    answer = _text(model.invoke([
        SystemMessage(content=(
            "你是商城客服。仅依据提供的证据回答，逐个事实附上 [编号]；证据不足时明确说无法核实。"
            "不得承诺具体到账时间、送达时间或赔付结果。"
        )),
        HumanMessage(content=json.dumps({"question": question, "evidence": evidence}, ensure_ascii=False)),
    ]).content).strip()
    verdict = _json_object(_text(model.invoke([
        SystemMessage(content=(
            "你是 Faithfulness 评审。逐条核查答案中的可验证事实是否被所给证据直接支持。"
            "只看证据，不使用常识。纯拒答且未添加事实时得 1。只输出合法 JSON，"
            '包含 0 到 1 的数字字段 "score" 和简短原因字段 "reason"。'
        )),
        HumanMessage(content=json.dumps({"answer": answer, "evidence": evidence}, ensure_ascii=False)),
    ]).content))
    score = float(verdict["score"])
    if not 0 <= score <= 1:
        raise ValueError("Faithfulness 分数超出 0..1")
    return answer, score, str(verdict.get("reason") or "")


def _average(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    return round(sum(values) / len(values), 4) if values else None


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(rows), "answerable": sum(bool(row["ground_truth"]) for row in rows),
            **{key: _average(rows, key) for key in
               ("recall@1", "recall@5", "recall@10", "mrr", "faithfulness")},
            "unanswerable_refusal_rate": _average(
                [row for row in rows if not row["ground_truth"]], "refused"
            )}


def _cases(dataset: Path) -> list[dict[str, Any]]:
    cases = [json.loads(line) for line in dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("评估集 id 重复")
    return cases


def _group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, Any] = {}
    for strategy in STRATEGIES:
        strategy_rows = [row for row in rows if row["strategy"] == strategy]
        grouped[strategy] = {
            "overall": _summary(strategy_rows),
            "by_type": {kind: _summary([row for row in strategy_rows if row["type"] == kind])
                        for kind in sorted({row["type"] for row in strategy_rows})},
            "by_difficulty": {level: _summary([row for row in strategy_rows if row["difficulty"] == level])
                              for level in ("easy", "medium", "hard")},
        }
    return grouped


def recompute_report(report: dict[str, Any], dataset: Path = DATASET) -> dict[str, Any]:
    """标注修正后用保存的召回列表重算检索指标，不重复请求模型或改动答案评审。"""
    cases = {case["id"]: case for case in _cases(dataset)}
    rows = report["cases"]
    expected = {(strategy, case_id) for strategy in STRATEGIES for case_id in cases}
    actual = [(row["strategy"], row["id"]) for row in rows]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError("已有报告的策略或题目与评估集不一致")
    for row in rows:
        case = cases[row["id"]]
        if row["query"] != case["query"]:
            raise ValueError(f"问题正文已变化，须重新检索：{row['id']}")
        row.update({key: case[key] for key in ("type", "difficulty", "ground_truth", "answer_facts")})
        row.update(_retrieval_numbers_from_rows(row["retrieved"], case["ground_truth"]))
        answer = row.get("answer")
        row["refused"] = (int(any(word in answer for word in
                          ("无法核实", "无法确认", "证据不足", "不能保证")))
                          if answer is not None and not case["ground_truth"] else None)
    report["dataset"] = str(dataset)
    report["strategies"] = _group(rows)
    return report


def run(*, dataset: Path = DATASET, generate: bool = True,
        collection: str | None = None,
        on_progress: Callable[[int, int, str, str], None] | None = None) -> dict[str, Any]:
    """每个策略跑同一组题；每完成一题通知进度，便于后台任务展示状态。"""
    from app.persistence.milvus.knowledge_store import KnowledgeVectorStore

    cases = _cases(dataset)
    model = _model() if generate else None
    store = KnowledgeVectorStore(collection=collection) if collection else None
    rows: list[dict[str, Any]] = []
    total = len(STRATEGIES) * len(cases)
    for strategy in STRATEGIES:
        for case in cases:
            print(f"[{strategy}] {case['id']}", file=sys.stderr, flush=True)
            hits = semantic_search(case["query"], strategy=strategy, limit=10, store=store)
            numbers = _retrieval_numbers(hits, case["ground_truth"])
            answer, faithfulness, judge_reason = (
                _answer_and_faithfulness(model, case["query"], hits)
                if model else (None, None, None)
            )
            refused = (int(any(word in answer for word in ("无法核实", "无法确认", "证据不足", "不能保证")))
                       if answer is not None and not case["ground_truth"] else None)
            rows.append({
                "strategy": strategy, "id": case["id"], "query": case["query"],
                "type": case["type"], "difficulty": case["difficulty"],
                "ground_truth": case["ground_truth"], "answer_facts": case["answer_facts"],
                "retrieved": [{"chunk_id": hit.chunk.id, "source_path": hit.chunk.source_path,
                               "section_path": hit.chunk.section_path, "score": hit.score}
                              for hit in hits],
                **numbers, "answer": answer, "faithfulness": faithfulness,
                "judge_reason": judge_reason, "refused": refused,
            })
            if on_progress:
                on_progress(len(rows), total, strategy, case["id"])
    return {"dataset": str(dataset), "collection": collection or "configured default",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "faithfulness_judge": settings.openai_model if generate else None,
            "strategies": _group(rows), "cases": rows}


def write_report(report: dict[str, Any], path: Path) -> None:
    """同目录写临时文件再替换，避免评估页面读到未写完的报告。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f"{path.stem}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(report, output, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--report", type=Path, default=Path("evals/reports/customer_rag_v1.json"))
    parser.add_argument("--collection", help="显式使用待评估的 Milvus 集合，不修改本地 .env")
    parser.add_argument("--retrieval-only", action="store_true", help="只计算检索指标")
    parser.add_argument("--recompute-from", type=Path, help="从已有 JSON 报告的召回结果按当前标注重算")
    args = parser.parse_args()
    report = (recompute_report(json.loads(args.recompute_from.read_text(encoding="utf-8")), args.dataset)
              if args.recompute_from else
              run(dataset=args.dataset, generate=not args.retrieval_only, collection=args.collection))
    write_report(report, args.report)
    print(json.dumps({"report": str(args.report), "strategies": report["strategies"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
