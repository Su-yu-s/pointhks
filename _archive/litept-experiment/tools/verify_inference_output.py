"""Verify LitePT schema-v1 inference artifacts and point-count invariants."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from plyfile import PlyData


SCHEMA_VERSION = 1
LABEL_DTYPE = np.dtype(
    [("semantic_id", "<i4"), ("instance_id", "<i4"), ("confidence", "<f4")]
)


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Required output file is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {path}: {exc}") from exc
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path.name} must use schema_version {SCHEMA_VERSION}.")
    return value


def verify_output(output_dir: Path) -> dict:
    output_dir = output_dir.resolve()
    run = _read_json(output_dir / "run.json")
    if run.get("status") != "success":
        raise ValueError(f"run.json status is not success: {run.get('status')}")
    _read_json(output_dir / "request.json")
    input_points = int(run["input_points"])
    valid_points = int(run["valid_points"])
    invalid_points = int(run["invalid_points"])
    if input_points != valid_points + invalid_points:
        raise ValueError("input_points must equal valid_points + invalid_points.")

    label_path = output_dir / "point_labels.bin"
    expected_label_bytes = input_points * LABEL_DTYPE.itemsize
    if not label_path.is_file() or label_path.stat().st_size != expected_label_bytes:
        actual = label_path.stat().st_size if label_path.is_file() else None
        raise ValueError(
            f"point_labels.bin size mismatch: expected {expected_label_bytes}, got {actual}."
        )
    labels = np.fromfile(label_path, dtype=LABEL_DTYPE)
    if not np.isfinite(labels["confidence"]).all():
        raise ValueError("point_labels.bin contains non-finite confidence values.")
    if np.any((labels["confidence"] < 0) | (labels["confidence"] > 1)):
        raise ValueError("point_labels.bin confidence must be in [0, 1].")

    index_path = output_dir / "source_index.bin"
    expected_index_bytes = valid_points * np.dtype("<i8").itemsize
    if not index_path.is_file() or index_path.stat().st_size != expected_index_bytes:
        actual = index_path.stat().st_size if index_path.is_file() else None
        raise ValueError(
            f"source_index.bin size mismatch: expected {expected_index_bytes}, got {actual}."
        )
    source_index = np.fromfile(index_path, dtype="<i8")
    if source_index.size and (
        source_index.min() < 0
        or source_index.max() >= input_points
        or np.unique(source_index).size != source_index.size
    ):
        raise ValueError("source_index.bin contains duplicate or out-of-range indices.")
    if not np.array_equal(labels["semantic_id"] < 0, ~np.isin(np.arange(input_points), source_index)):
        raise ValueError("Invalid-point semantic labels do not match source_index.bin.")

    ply_path = output_dir / "labeled_cloud.ply"
    if not ply_path.is_file():
        raise FileNotFoundError(f"Required output file is missing: {ply_path}")
    ply_points = int(PlyData.read(ply_path)["vertex"].count)
    if ply_points != valid_points:
        raise ValueError(f"labeled_cloud.ply has {ply_points} points; expected {valid_points}.")

    task = run.get("task", "instance_segmentation")
    instance_count = 0
    if task == "instance_segmentation":
        instance_document = _read_json(output_dir / "instances.json")
        instances = instance_document.get("instances")
        if not isinstance(instances, list):
            raise ValueError("instances.json must contain an instances list.")
        instance_count = len(instances)
        expected_ids = set(range(instance_count))
        actual_ids = set(int(value) for value in np.unique(labels["instance_id"]) if value >= 0)
        if actual_ids != expected_ids:
            raise ValueError(
                f"Instance IDs are not contiguous or do not match instances.json: {sorted(actual_ids)}."
            )
        for instance in instances:
            instance_id = int(instance["instance_id"])
            actual_points = int(np.count_nonzero(labels["instance_id"] == instance_id))
            if int(instance["points"]) != actual_points:
                raise ValueError(
                    f"Instance {instance_id} point count mismatch: JSON={instance['points']}, labels={actual_points}."
                )
    elif np.any(labels["instance_id"] >= 0):
        raise ValueError("Semantic-only output must use instance_id=-1 for every point.")

    temporary_files = [
        str(path.relative_to(output_dir))
        for path in output_dir.rglob(".*.tmp-*")
    ]
    if temporary_files:
        raise ValueError(f"Temporary output files remain: {temporary_files}")
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "output_dir": str(output_dir),
        "task": task,
        "input_points": input_points,
        "valid_points": valid_points,
        "invalid_points": invalid_points,
        "instances": instance_count,
        "checks": [
            "schema", "point_counts", "binary_sizes", "confidence_range",
            "source_index_mapping", "ply_point_count", "instance_consistency", "atomic_cleanup",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify_output(args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
