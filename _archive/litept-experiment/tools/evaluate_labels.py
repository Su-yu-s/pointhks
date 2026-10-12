"""Evaluate semantic and class-aware instance labels using schema-v1 BIN files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


SCHEMA_VERSION = 1
LABEL_DTYPE = np.dtype(
    [("semantic_id", "<i4"), ("instance_id", "<i4"), ("confidence", "<f4")]
)


def load_labels(path: Path) -> np.ndarray:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size % LABEL_DTYPE.itemsize:
        raise ValueError(f"{path} is not a schema-v1 label BIN file.")
    return np.fromfile(path, dtype=LABEL_DTYPE)


def evaluate(prediction: np.ndarray, ground_truth: np.ndarray, instance_iou: float = 0.5) -> dict:
    if prediction.shape != ground_truth.shape:
        raise ValueError(
            f"Prediction and ground truth record counts differ: {prediction.size} vs {ground_truth.size}."
        )
    valid = ground_truth["semantic_id"] >= 0
    if not valid.any():
        raise ValueError("Ground truth contains no valid semantic labels.")
    pred_sem = prediction["semantic_id"][valid]
    gt_sem = ground_truth["semantic_id"][valid]
    classes = sorted(set(int(value) for value in np.unique(gt_sem)))
    per_class = []
    for class_id in classes:
        pred_mask = pred_sem == class_id
        gt_mask = gt_sem == class_id
        intersection = int(np.count_nonzero(pred_mask & gt_mask))
        union = int(np.count_nonzero(pred_mask | gt_mask))
        per_class.append(
            {
                "semantic_id": class_id,
                "ground_truth_points": int(gt_mask.sum()),
                "iou": intersection / union if union else 0.0,
            }
        )
    semantic = {
        "evaluated_points": int(valid.sum()),
        "overall_accuracy": float(np.mean(pred_sem == gt_sem)),
        "mean_iou": float(np.mean([value["iou"] for value in per_class])),
        "per_class": per_class,
    }

    gt_instances = [int(value) for value in np.unique(ground_truth["instance_id"]) if value >= 0]
    pred_instances = [int(value) for value in np.unique(prediction["instance_id"]) if value >= 0]
    candidates = []
    for gt_id in gt_instances:
        gt_mask = ground_truth["instance_id"] == gt_id
        gt_class = int(np.bincount(ground_truth["semantic_id"][gt_mask]).argmax())
        for pred_id in pred_instances:
            pred_mask = prediction["instance_id"] == pred_id
            pred_class_values = prediction["semantic_id"][pred_mask]
            if pred_class_values.size == 0:
                continue
            pred_class = int(np.bincount(pred_class_values).argmax())
            if pred_class != gt_class:
                continue
            intersection = int(np.count_nonzero(gt_mask & pred_mask))
            if not intersection:
                continue
            union = int(np.count_nonzero(gt_mask | pred_mask))
            candidates.append((intersection / union, gt_id, pred_id))
    matched_gt, matched_pred = set(), set()
    matches = []
    for iou, gt_id, pred_id in sorted(candidates, reverse=True):
        if iou < instance_iou or gt_id in matched_gt or pred_id in matched_pred:
            continue
        matched_gt.add(gt_id)
        matched_pred.add(pred_id)
        matches.append({"ground_truth_id": gt_id, "prediction_id": pred_id, "iou": iou})
    tp = len(matches)
    fp = len(pred_instances) - tp
    fn = len(gt_instances) - tp
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    instance = {
        "iou_threshold": instance_iou,
        "ground_truth_instances": len(gt_instances),
        "predicted_instances": len(pred_instances),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "matches": matches,
    }
    return {"schema_version": SCHEMA_VERSION, "semantic": semantic, "instance": instance}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prediction", type=Path)
    parser.add_argument("ground_truth", type=Path)
    parser.add_argument("--instance-iou", type=float, default=0.5)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 < args.instance_iou <= 1:
        raise ValueError("--instance-iou must be in (0, 1].")
    result = evaluate(load_labels(args.prediction), load_labels(args.ground_truth), args.instance_iou)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
