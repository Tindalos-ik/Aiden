"""结构检查 topic synthetic pending-review dataset; never trains or calls a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
import sys


BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.services.topic_classification.data import (
    INPUT_VERSION, fingerprint, input_hash, prepare_dataset, read_jsonl, validate_dataset,
)
from app.services.topic_classification.taxonomy import LABEL_IDS, TAXONOMY_VERSION, validate_labels
def secret_field_paths(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if str(key).lower() in {"api_key", "authorization", "access_token", "base_url", "endpoint", "secret", "credential", "token"}:
                yield path
            yield from secret_field_paths(child, path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from secret_field_paths(child, f"{prefix}[{index}]")

SPLITS = ("train", "validation", "test")
SENSITIVE = re.compile(r"\d{10,}|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PLACEHOLDERS = re.compile(r"(?:TODO|待填写|待补充|请输入.*答案|在此输入|[<\[](?:answer|question|商品名|问题)[>\]])", re.I)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_dataset(root: Path) -> tuple[dict, dict]:
    findings = []
    errors = []
    dataset = load_json(root / "dataset.json")
    manifest = load_json(root / "manifest.json")
    splits = {}
    for split in SPLITS:
        path = root / f"{split}.jsonl"
        rows = []
        try:
            rows = read_jsonl(path)
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"{path.name}: existing read_jsonl failed: {type(exc).__name__}: {exc}")
        # read_jsonl skips blank lines, so independently reject non-JSONL formatting.
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                errors.append(f"{path.name}:{number}: blank JSONL line")
            else:
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{path.name}:{number}: invalid JSON ({exc.msg})")
        splits[split] = rows
        if rows != dataset.get("splits", {}).get(split):
            errors.append(f"{split}: JSONL differs from dataset.json split")
        if fingerprint(rows) != dataset.get("split_hashes", {}).get(split):
            errors.append(f"{split}: split hash mismatch")
    try:
        provenance = read_jsonl(root / "provenance.jsonl")
    except (OSError, ValueError) as exc:
        provenance = []
        errors.append(f"provenance.jsonl unreadable: {exc}")
    prov_by_id = {}
    for row in provenance:
        sample_id = row.get("sample_id", row.get("id"))
        if not sample_id or sample_id in prov_by_id:
            errors.append("provenance has missing or duplicate sample_id")
        else:
            prov_by_id[sample_id] = row

    all_rows = []
    group_splits = defaultdict(set)
    exact = defaultdict(list)
    exact_pairs = []
    label_rows = Counter()
    label_groups = {label: set() for label in LABEL_IDS}
    combos = Counter()
    split_groups = {}
    language_forms = Counter()
    products = Counter()
    batch_counts = Counter()
    worker_models = Counter()
    worker_flavors = set()
    multi_hot_checked = 0
    for split, rows in splits.items():
        split_groups[split] = {str(row.get("group_id", "")) for row in rows}
        for row in rows:
            all_rows.append(row)
            sample_id = row.get("id")
            text = row.get("original_question")
            labels = row.get("labels")
            if not sample_id or not isinstance(text, str) or not text.strip():
                errors.append(f"{split}: missing id or nonempty original_question")
                continue
            try:
                normalized_labels = validate_labels(labels)
                if labels != normalized_labels:
                    errors.append(f"{sample_id}: labels are not in canonical taxonomy order")
            except (TypeError, ValueError) as exc:
                errors.append(f"{sample_id}: invalid labels ({exc})")
                normalized_labels = []
            target = [int(label in normalized_labels) for label in LABEL_IDS]
            decoded = [label for label, bit in zip(LABEL_IDS, target) if bit]
            if decoded != normalized_labels or len(target) != 17:
                errors.append(f"{sample_id}: canonical 17-label multi-hot encoding mismatch")
            multi_hot_checked += 1
            if len(normalized_labels) > 3:
                errors.append(f"{sample_id}: more than three labels")
            allowed_statuses = {"predicted"} if labels else {"uncertain", "insufficient_context"}
            if row.get("classification_status") not in allowed_statuses:
                errors.append(f"{sample_id}: status/label mismatch")
            if row.get("annotation_status") != "prelabel":
                errors.append(f"{sample_id}: annotation_status is not prelabel")
            if row.get("privacy_confirmed") is not False:
                errors.append(f"{sample_id}: synthetic privacy_confirmed must remain false")
            if row.get("input_hash") != input_hash(text):
                errors.append(f"{sample_id}: input_hash mismatch")
            group_id = str(row.get("group_id", ""))
            if not group_id:
                errors.append(f"{sample_id}: missing group_id")
            group_splits[group_id].add(split)
            exact[re.sub(r"\s+", "", text).lower()].append((sample_id, split))
            label_rows.update(normalized_labels)
            for label in normalized_labels:
                label_groups[label].add(group_id)
            combos["+".join(normalized_labels) if normalized_labels else "<empty>"] += 1
            if SENSITIVE.search(text):
                findings.append({"kind": "sensitive_pattern", "sample_id": sample_id, "split": split})
            if PLACEHOLDERS.search(text):
                findings.append({"kind": "placeholder_or_instruction", "sample_id": sample_id, "split": split})
            meta = prov_by_id.get(sample_id)
            if not meta:
                errors.append(f"{sample_id}: missing provenance sidecar row")
                continue
            if meta.get("source") != "synthetic" or meta.get("human_reviewed") is not False:
                errors.append(f"{sample_id}: provenance must say synthetic and human_reviewed=false")
            if meta.get("privacy_confirmed") is not False:
                errors.append(f"{sample_id}: provenance privacy_confirmed must remain false")
            if meta.get("split") != split:
                errors.append(f"{sample_id}: provenance split mismatch")
            author = meta.get("author")
            if not isinstance(author, dict) or not author.get("flavor") or not author.get("model"):
                errors.append(f"{sample_id}: provenance is missing recorded author flavor/model")
            else:
                worker_flavors.add(str(author["flavor"]))
                worker_models[f"{author['flavor']}:{author['model']}"] += 1
                if author["flavor"] not in {"fast", "good"} or author["model"] not in {"openai-codex/gpt-6.1-sol:medium", "openai-codex/gpt-6-luna:medium"}:
                    errors.append(f"{sample_id}: unrecognized worker flavor/model metadata")
            origin = meta.get("origin_group_id", meta.get("group_id"))
            if origin != group_id:
                errors.append(f"{sample_id}: canonical origin_group_id/group_id mismatch")
            if not meta.get("generation_batch"):
                errors.append(f"{sample_id}: missing generation_batch")
            if row.get("taxonomy_version") != TAXONOMY_VERSION or row.get("input_version") != INPUT_VERSION:
                errors.append(f"{sample_id}: taxonomy/input version mismatch")
            batch_counts[str(meta.get("generation_batch", ""))] += 1
            forms = meta.get("language_forms", {})
            if isinstance(forms, dict):
                language_forms.update(map(str, forms.get("forms", [])))
            elif isinstance(forms, list):
                language_forms.update(map(str, forms))
            elif forms:
                language_forms.update(map(str, forms))
            products[str(meta.get("product", "unknown"))] += 1

    semantic = dataset.get("semantic_review", {})
    if semantic.get("confirmed") is not False:
        errors.append("dataset semantic_review.confirmed must remain false pending human review")
    # The scene manifest is persisted before expression generation; cross-check lineage.
    scene_rows = read_jsonl(root / "scenes.jsonl")
    scene_manifest = load_json(root / "scene_split_manifest.json")
    if not scene_rows or not scene_manifest:
        errors.append("scenes.jsonl and scene_split_manifest.json must be populated")
    scene_ids = {str(row.get("scenario_id", "")) for row in scene_rows}
    if len(scene_ids) != len(scene_rows) or "" in scene_ids:
        errors.append("scene scenario_id values are missing or duplicated")
    scene_splits = scene_manifest.get("origins", {})
    if not scene_splits:
        errors.append("scene split manifest has no origins mapping")
    scene_digest = fingerprint(scene_rows)
    manifest_payload = {key: value for key, value in scene_manifest.items() if key != "manifest_hash"}
    if scene_manifest.get("scenes_hash") != scene_digest or scene_manifest.get("manifest_hash") != fingerprint(manifest_payload):
        errors.append("frozen scene split manifest/hash lineage mismatch")
    if set(scene_splits) != scene_ids:
        errors.append("frozen scene origins do not match scenario_id set")
    for scene in scene_rows:
        scenario_id = scene.get("scenario_id")
        if scenario_id not in scene_splits:
            errors.append(f"scene has no pre-expression split assignment: {scenario_id}")
        elif scene_splits[scenario_id].get("split") not in SPLITS:
            errors.append(f"scene has invalid pre-expression split: {scenario_id}")
    final_manifest = load_json(root / "final_split_manifest.json")
    final_origins = final_manifest.get("origins", {})
    for sample_id, meta in prov_by_id.items():
        scenario_id = meta.get("scenario_id")
        allocation = final_origins.get(scenario_id, {})
        if meta.get("origin_group_id") != meta.get("group_id") or not allocation or allocation.get("group_id") != meta.get("group_id") or allocation.get("split") != meta.get("split"):
            errors.append(f"{sample_id}: persisted final split lineage mismatch")
    by_id = {row.get("id"): row for row in all_rows}
    originals = [{key: value for key, value in by_id[sample_id].items() if key != "group_id"} for sample_id in dataset.get("original_ids", []) if sample_id in by_id]
    if len(originals) != len(all_rows) or fingerprint(originals) != dataset.get("dataset_hash") or dataset.get("semantic_review", {}).get("dataset_hash") != dataset.get("dataset_hash"):
        errors.append("dataset/semantic-review hash lineage mismatch")
    if manifest.get("split_hashes") != dataset.get("split_hashes") or manifest.get("dataset_hash") != dataset.get("dataset_hash"):
        errors.append("manifest/native dataset hash lineage mismatch")
    status = load_json(root / "status.json")
    if status.get("phase") != "complete" or status.get("external_provider_calls") != 0:
        errors.append("generation status must report completed phase and zero external provider calls")
    if not {"fast", "good"}.issubset(worker_flavors):
        errors.append("provenance must record both fast and good worker authors")
    if manifest.get("delivery_status") != "pending_human_review" or dataset.get("delivery_status") != "pending_human_review":
        errors.append("dataset delivery status must remain pending_human_review")
    if manifest.get("sample_count") != len(all_rows) or status.get("retained") != len(all_rows):
        errors.append("manifest/status sample count mismatch")
    if manifest.get("human_reviewed") is not False or manifest.get("privacy_confirmed") is not False or manifest.get("semantic_review_confirmed") is not False:
        errors.append("manifest must preserve all pending-human-review gates as false")
    file_hashes = manifest.get("file_sha256", {})
    for relative, expected_hash in file_hashes.items():
        file_path = root / relative
        if not file_path.is_file() or sha256_file(file_path) != expected_hash:
            errors.append(f"manifest file checksum mismatch: {relative}")
    for raw_path in (root / "raw").glob("*.json"):
        raw_record = load_json(raw_path)
        secrets = list(secret_field_paths(raw_record))
        if secrets:
            errors.append(f"{raw_path.relative_to(root)}: sensitive configuration fields present: {secrets}")
    challenge = read_jsonl(root / "ambiguity_challenge.jsonl")
    challenge_ids = set()
    supervised_hashes = {row.get("input_hash") for row in all_rows}
    challenge_hashes = set()
    for row in challenge:
        sample_id = row.get("id")
        text = row.get("original_question")
        if not sample_id or sample_id in challenge_ids or sample_id in prov_by_id:
            errors.append(f"{sample_id}: challenge id is missing or duplicated")
        challenge_ids.add(sample_id)
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{sample_id}: challenge question is empty")
        expected_hash = input_hash(text) if isinstance(text, str) else None
        if row.get("input_hash") != expected_hash or expected_hash in challenge_hashes or expected_hash in supervised_hashes:
            errors.append(f"{sample_id}: challenge input hash mismatch or duplicate")
        challenge_hashes.add(expected_hash)
        if row.get("labels") != [] or row.get("classification_status") not in {"uncertain", "insufficient_context"}:
            errors.append(f"{sample_id}: challenge row must preserve empty labels and source uncertainty status")
        if row.get("source") != "synthetic" or row.get("human_reviewed") is not False or row.get("privacy_confirmed") is not False:
            errors.append(f"{sample_id}: challenge row must remain synthetic and pending human privacy review")
        if row.get("annotation_status") != "prelabel":
            errors.append(f"{sample_id}: challenge annotation_status must remain prelabel")

    extra_prov = set(prov_by_id) - {row.get("id") for row in all_rows}
    if extra_prov:
        errors.append(f"provenance has {len(extra_prov)} orphan sample rows")
    if any(len(splits_for_group) > 1 for splits_for_group in group_splits.values()):
        errors.append("group_id crosses splits")
    over_origin = {group: count for group, count in Counter(str(r.get("group_id", "")) for r in all_rows).items() if count > 5}
    if over_origin:
        errors.append(f"origin groups over five rows: {len(over_origin)}")
    for values in exact.values():
        if len(values) > 1:
            exact_pairs.append({"sample_ids": [v[0] for v in values], "splits": sorted({v[1] for v in values})})
            if len({v[1] for v in values}) > 1:
                errors.append("normalized exact duplicate crosses splits")
    near_pairs = []

    row_split = {row.get("id"): split for split, rows in splits.items() for row in rows}
    for i, left in enumerate(all_rows):
        a = re.sub(r"\s+", "", str(left.get("original_question", ""))).lower()
        for right in all_rows[:i]:
            if row_split.get(left.get("id")) == row_split.get(right.get("id")):
                continue
            b = re.sub(r"\s+", "", str(right.get("original_question", ""))).lower()
            if not a or not b:
                continue
            matcher = SequenceMatcher(None, a, b)
            if matcher.quick_ratio() >= .9:
                score = matcher.ratio()
                if score >= .9:
                    near_pairs.append({"left_id": right.get("id"), "right_id": left.get("id"), "similarity": round(score, 4), "left_split": row_split.get(right.get("id")), "right_split": row_split.get(left.get("id"))})
    if near_pairs:
        errors.append(f"{len(near_pairs)} cross-split near-duplicate pairs require review")
    # Probe both existing gates without mutating or bypassing them.
    gate = {"prepare_dataset": "not_run", "validate_dataset": "not_run", "expected_pending_human_review_blocker": None}
    for gate_name, operation in (("validate_dataset", lambda: validate_dataset(dataset)), ("prepare_dataset", lambda: prepare_dataset(all_rows, semantic_review=semantic))):
        try:
            operation()
            gate[gate_name] = "passed"
        except Exception as exc:
            gate[gate_name] = f"blocked: {type(exc).__name__}: {exc}"
            if "human privacy confirmation required" in str(exc):
                gate["expected_pending_human_review_blocker"] = str(exc)
            else:
                errors.append(f"native {gate_name} gate blocked unexpectedly: {type(exc).__name__}: {exc}")

    coverage_thresholds = {label: {"distinct_groups": len(label_groups[label]), "minimum": 100, "passed": len(label_groups[label]) >= 100} for label in LABEL_IDS}
    if any(not item["passed"] for item in coverage_thresholds.values()):
        errors.append("one or more labels have fewer than 100 distinct groups")

    per_split = {}
    for split, rows in splits.items():
        per_split[split] = {"samples": len(rows), "groups": len(split_groups[split]), "labels": {label: sum(label in (r.get("labels") or []) for r in rows) for label in LABEL_IDS}, "distinct_groups_per_label": {label: len({r.get("group_id") for r in rows if label in (r.get("labels") or [])}) for label in LABEL_IDS}}
    cardinality = Counter(len(row.get("labels", [])) for row in all_rows)
    total = len(all_rows)
    stats = {
        "total_samples": total,
        "total_groups": len({row.get("group_id") for row in all_rows}),
        "per_split": per_split,
        "samples_per_label": {label: label_rows[label] for label in LABEL_IDS},
        "distinct_groups_per_label": {label: len(label_groups[label]) for label in LABEL_IDS},
        "coverage_thresholds": coverage_thresholds,
        "label_cardinality": {str(n): cardinality[n] for n in (0, 1, 2, 3)},
        "label_cardinality_ratio": {str(n): cardinality[n] / total if total else None for n in (0, 1, 2, 3)},
        "label_combinations": dict(combos),
        "language_forms_metadata": dict(language_forms),
        "products": dict(products),
        "generation_batches": dict(batch_counts),
        "ambiguity_challenge_rows": len(challenge),
        "multi_hot_encoding_checked_rows": multi_hot_checked,
        "normalized_exact_duplicate_groups": exact_pairs,
        "cross_split_near_duplicate_pairs": near_pairs,
        "similarity_method": "all cross-split pairs; length-bound and SequenceMatcher.quick_ratio upper-bound pruning; ratio >= 0.9",
        "automated_review_risks": findings,
        "native_loader": {
            "read_jsonl_each_split": {split: {"status": "passed", "rows": len(splits[split])} for split in SPLITS},
            "taxonomy_version": dataset.get("taxonomy_version"),
            "input_version": dataset.get("input_version"),
            "taxonomy_label_order": list(LABEL_IDS),
            "native_prepare_and_validate": gate,
        },
        "recorded_worker_models": dict(worker_models),
    }
    audit_rows = sorted(all_rows, key=lambda row: hashlib.sha256(str(row.get("id", "")).encode()).hexdigest())[:100]
    sample_flags = {item["sample_id"]: item["kind"] for item in findings}
    sampled_audit = [{"sample_id": row.get("id"), "split": row_split.get(row.get("id")), "labels": row.get("labels", []), "input_hash": row.get("input_hash"), "automated_flags": [sample_flags[row["id"]]] if row.get("id") in sample_flags else []} for row in audit_rows]
    stats["sampled_audit"] = {"method": "deterministic SHA-256(sample_id) ordering, first 100; automated screen only, not human semantic review", "rows": sampled_audit}
    safe_manifest = {key: manifest.get(key) for key in ("status", "counters", "dataset_hash", "split_hashes", "generation_batches") if key in manifest}
    report = {"status": "passed" if not errors else "failed", "errors": errors, "statistics": stats, "manifest_summary": safe_manifest}
    return report, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 pending-human-review synthetic topic dataset; 不训练、不调用模型。")
    parser.add_argument("--dataset-dir", required=True, type=Path)
    parser.add_argument("--report-dir", type=Path, help="报告输出目录；默认数据集目录，所有文件均拒绝覆盖。")
    args = parser.parse_args()
    root = args.dataset_dir.resolve()
    report, stats = check_dataset(root)
    report_dir = args.report_dir.resolve() if args.report_dir else root
    report_dir.mkdir(parents=True, exist_ok=True)
    outputs = (("statistics.json", stats), ("quality_report.json", report))
    existing = [name for name, _ in outputs if (report_dir / name).exists()]
    if existing:
        parser.error(f"refusing to overwrite existing reports: {', '.join(existing)}")
    for name, value in outputs:
        with (report_dir / name).open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"status": report["status"], "total_samples": stats["total_samples"], "errors": report["errors"], "human_gate": stats["native_loader"]["native_prepare_and_validate"]}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
