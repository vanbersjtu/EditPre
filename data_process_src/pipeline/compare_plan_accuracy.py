#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compare semantic plan accuracy against a ground-truth directory.

Metric: item-level role accuracy and coverage.
Also reports textbox-level precision/recall/F1 based on item grouping.
"""

import argparse
import json
import re
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Tuple


NATURAL_SORT_RE = re.compile(r"(\d+)")


def natural_sort_key(path: Path) -> List[object]:
    parts = NATURAL_SORT_RE.split(path.name)
    key: List[object] = []
    for part in parts:
        key.append(int(part) if part.isdigit() else part.lower())
    return key


def load_json(path: Path) -> Optional[Dict[str, object]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[WARN] Failed to load {path}: {exc}")
        return None


def extract_item_roles(plan: Dict[str, object]) -> Tuple[Dict[str, str], int]:
    roles: Dict[str, str] = {}
    duplicates = 0
    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") != "textbox":
            continue
        item_ids = node.get("item_ids")
        if not isinstance(item_ids, list):
            continue
        role = node.get("role") if isinstance(node.get("role"), str) else "unknown"
        for item_id in item_ids:
            if not isinstance(item_id, str):
                continue
            if item_id in roles:
                duplicates += 1
                continue
            roles[item_id] = role
    return roles, duplicates


def extract_textboxes(plan: Dict[str, object]) -> List[FrozenSet[str]]:
    boxes: List[FrozenSet[str]] = []
    nodes = plan.get("nodes") if isinstance(plan.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") != "textbox":
            continue
        item_ids = node.get("item_ids")
        if not isinstance(item_ids, list):
            continue
        cleaned = [item_id for item_id in item_ids if isinstance(item_id, str)]
        if not cleaned:
            continue
        boxes.append(frozenset(cleaned))
    return boxes


def compare_roles(
    gt_roles: Dict[str, str],
    pred_roles: Dict[str, str],
) -> Tuple[int, int, int, int]:
    total = len(gt_roles)
    covered = 0
    correct = 0
    for item_id, gt_role in gt_roles.items():
        pred_role = pred_roles.get(item_id)
        if pred_role is None:
            continue
        covered += 1
        if pred_role == gt_role:
            correct += 1
    extra = sum(1 for item_id in pred_roles if item_id not in gt_roles)
    return total, covered, correct, extra


def compare_textboxes(
    gt_boxes: List[FrozenSet[str]],
    pred_boxes: List[FrozenSet[str]],
) -> Tuple[int, int, int]:
    gt_counts: Dict[FrozenSet[str], int] = {}
    pred_counts: Dict[FrozenSet[str], int] = {}
    for box in gt_boxes:
        gt_counts[box] = gt_counts.get(box, 0) + 1
    for box in pred_boxes:
        pred_counts[box] = pred_counts.get(box, 0) + 1

    tp = 0
    for box, gt_count in gt_counts.items():
        pred_count = pred_counts.get(box, 0)
        tp += min(gt_count, pred_count)

    fp = len(pred_boxes) - tp
    fn = len(gt_boxes) - tp
    return tp, fp, fn


def parse_pred_args(values: Iterable[str]) -> List[Tuple[str, Path]]:
    preds: List[Tuple[str, Path]] = []
    for value in values:
        if "=" in value:
            label, path_str = value.split("=", 1)
        else:
            path_str = value
            label = Path(path_str).name
        preds.append((label, Path(path_str)))
    return preds


def summarize_totals(label: str, totals: Dict[str, int]) -> None:
    total_items = totals["total_items"]
    covered_items = totals["covered_items"]
    correct_items = totals["correct_items"]
    extra_items = totals["extra_items"]
    duplicates = totals["duplicates"]
    missing_slides = totals["missing_slides"]

    accuracy = (correct_items / total_items) if total_items else 0.0
    coverage = (covered_items / total_items) if total_items else 0.0
    tb_tp = totals["tb_tp"]
    tb_fp = totals["tb_fp"]
    tb_fn = totals["tb_fn"]
    tb_prec = tb_tp / (tb_tp + tb_fp) if (tb_tp + tb_fp) else 0.0
    tb_rec = tb_tp / (tb_tp + tb_fn) if (tb_tp + tb_fn) else 0.0
    tb_f1 = (2 * tb_prec * tb_rec / (tb_prec + tb_rec)) if (tb_prec + tb_rec) else 0.0

    print(
        f"[SUMMARY] {label}: total_items={total_items}, "
        f"accuracy={accuracy:.3f}, coverage={coverage:.3f}, "
        f"extra_items={extra_items}, duplicates={duplicates}, missing_slides={missing_slides}, "
        f"tb_precision={tb_prec:.3f}, tb_recall={tb_rec:.3f}, tb_f1={tb_f1:.3f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare semantic plan accuracy against ground truth.",
    )
    parser.add_argument("--gt", required=True, help="Ground-truth plans directory.")
    parser.add_argument(
        "--pred",
        action="append",
        required=True,
        help="Prediction plans directory. Use label=path to name it.",
    )
    args = parser.parse_args()

    gt_dir = Path(args.gt)
    pred_dirs = parse_pred_args(args.pred)

    gt_files = sorted(gt_dir.glob("*.json"), key=natural_sort_key)
    if not gt_files:
        print(f"No ground-truth plans found in {gt_dir}")
        return

    totals = {
        label: {
            "total_items": 0,
            "covered_items": 0,
            "correct_items": 0,
            "extra_items": 0,
            "duplicates": 0,
            "missing_slides": 0,
            "tb_tp": 0,
            "tb_fp": 0,
            "tb_fn": 0,
        }
        for label, _ in pred_dirs
    }

    header = ["slide", "items", "tb_items"]
    for label, _ in pred_dirs:
        header.extend(
            [
                f"{label}_acc",
                f"{label}_cov",
                f"{label}_extra",
                f"{label}_tb_prec",
                f"{label}_tb_rec",
                f"{label}_tb_f1",
            ]
        )
    print("\t".join(header))

    for gt_path in gt_files:
        gt_plan = load_json(gt_path)
        if gt_plan is None:
            continue
        gt_roles, _ = extract_item_roles(gt_plan)
        gt_boxes = extract_textboxes(gt_plan)
        row = [gt_path.stem, str(len(gt_roles)), str(len(gt_boxes))]

        for label, pred_dir in pred_dirs:
            pred_path = pred_dir / gt_path.name
            if not pred_path.exists():
                totals[label]["missing_slides"] += 1
                row.extend(["NA", "NA", "NA", "NA", "NA", "NA"])
                continue

            pred_plan = load_json(pred_path)
            if pred_plan is None:
                totals[label]["missing_slides"] += 1
                row.extend(["NA", "NA", "NA", "NA", "NA", "NA"])
                continue

            pred_roles, duplicates = extract_item_roles(pred_plan)
            total, covered, correct, extra = compare_roles(gt_roles, pred_roles)
            pred_boxes = extract_textboxes(pred_plan)
            tb_tp, tb_fp, tb_fn = compare_textboxes(gt_boxes, pred_boxes)

            totals[label]["total_items"] += total
            totals[label]["covered_items"] += covered
            totals[label]["correct_items"] += correct
            totals[label]["extra_items"] += extra
            totals[label]["duplicates"] += duplicates
            totals[label]["tb_tp"] += tb_tp
            totals[label]["tb_fp"] += tb_fp
            totals[label]["tb_fn"] += tb_fn

            acc = (correct / total) if total else 0.0
            cov = (covered / total) if total else 0.0
            tb_prec = tb_tp / (tb_tp + tb_fp) if (tb_tp + tb_fp) else 0.0
            tb_rec = tb_tp / (tb_tp + tb_fn) if (tb_tp + tb_fn) else 0.0
            tb_f1 = (2 * tb_prec * tb_rec / (tb_prec + tb_rec)) if (tb_prec + tb_rec) else 0.0
            row.extend(
                [
                    f"{acc:.3f}",
                    f"{cov:.3f}",
                    str(extra),
                    f"{tb_prec:.3f}",
                    f"{tb_rec:.3f}",
                    f"{tb_f1:.3f}",
                ]
            )

        print("\t".join(row))

    for label, _ in pred_dirs:
        summarize_totals(label, totals[label])


if __name__ == "__main__":
    main()
