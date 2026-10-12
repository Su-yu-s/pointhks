"""Run stable NuScenes semantic inference with the released LitePT checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
import torch
from plyfile import PlyData, PlyElement

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datasets.transform import Compose
from models import build_model
from utils.config import Config


SCHEMA_VERSION = 1
DEFAULT_CONFIG = ROOT / "configs" / "nuscenes" / "semseg-litept-small-v1m1.py"
DEFAULT_WEIGHT = ROOT / "weights" / "model_best.pth"
LABEL_DTYPE = np.dtype(
    [("semantic_id", "<i4"), ("instance_id", "<i4"), ("confidence", "<f4")]
)
COLORS = np.array(
    [
        (112, 128, 144), (220, 20, 60), (255, 127, 80), (0, 0, 142),
        (70, 70, 70), (255, 0, 0), (255, 158, 0), (255, 61, 99),
        (0, 0, 110), (0, 0, 70), (0, 207, 191), (175, 0, 75),
        (75, 0, 75), (152, 251, 152), (70, 130, 180), (0, 175, 0),
    ],
    dtype=np.uint8,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="NuScenes float32 N x 5 .bin file.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "nuscenes_semantic")
    parser.add_argument("--weight", type=Path, default=DEFAULT_WEIGHT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Replace known LitePT artifacts in a non-empty output directory.",
    )
    parser.add_argument("--debug", action="store_true", help="Show a Python traceback on failure.")
    parser.add_argument("--grid-size", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--invalid-policy", choices=("drop", "error"), default="drop",
        help="Drop NaN/Inf records with source-index preservation, or fail.",
    )
    return parser.parse_args()


def _temporary_output_path(path: Path) -> Path:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    return temporary


def write_json(path: Path, value: object) -> None:
    temporary = _temporary_output_path(path)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def write_binary(path: Path, value: np.ndarray) -> None:
    temporary = _temporary_output_path(path)
    value.tofile(temporary)
    temporary.replace(path)


def prepare_output_dir(path: Path, overwrite: bool) -> None:
    known_files = {
        "request.json", "run.json", "point_labels.bin", "source_index.bin", "labeled_cloud.ply",
    }
    if path.exists() and any(path.iterdir()):
        if not overwrite:
            raise FileExistsError(
                f"Output directory is not empty: {path}. Choose a new directory or use --overwrite-output."
            )
        for name in known_files:
            target = path / name
            if target.is_file():
                target.unlink()
        instance_dir = path / "instances"
        if instance_dir.is_dir():
            shutil.rmtree(instance_dir)
        for temporary in path.glob(".*.tmp-*"):
            if temporary.is_file():
                temporary.unlink()
    path.mkdir(parents=True, exist_ok=True)


def emit_progress(stage: str, progress: float, **details: object) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "progress": round(float(progress), 4),
        **details,
    }
    print("CLOUDFORGE_EVENT " + json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def load_nuscenes_bin(path: Path, invalid_policy: str = "drop") -> tuple[np.ndarray, np.ndarray, int]:
    if not path.is_file():
        raise FileNotFoundError(f"Input point cloud not found: {path}")
    byte_size = path.stat().st_size
    if byte_size == 0:
        raise ValueError(f"Input point cloud is empty: {path}")
    record_bytes = 5 * np.dtype("<f4").itemsize
    if byte_size % record_bytes:
        raise ValueError(
            f"NuScenes .bin must contain little-endian float32 N x 5 records; "
            f"file size {byte_size} is not divisible by {record_bytes}."
        )
    raw = np.fromfile(path, dtype="<f4").reshape(-1, 5)
    valid = np.isfinite(raw).all(axis=1)
    invalid_count = int((~valid).sum())
    if invalid_count and invalid_policy == "error":
        first = int(np.flatnonzero(~valid)[0])
        raise ValueError(f"Input contains {invalid_count} NaN/Inf records; first invalid index is {first}.")
    source_index = np.flatnonzero(valid).astype(np.int64)
    raw = raw[valid]
    if raw.shape[0] == 0:
        raise ValueError("Input contains no valid points after removing NaN/Inf records.")
    return raw, source_index, invalid_count


def load_model(config_path: Path, weight_path: Path) -> tuple[torch.nn.Module, Config]:
    if not config_path.is_file():
        raise FileNotFoundError(f"Model config not found: {config_path}")
    if not weight_path.is_file():
        raise FileNotFoundError(f"Model checkpoint not found: {weight_path}")
    config = Config.fromfile(config_path)
    model = build_model(config.model).cuda().eval()
    checkpoint = torch.load(weight_path, map_location="cpu", weights_only=False)
    if "state_dict" not in checkpoint:
        raise ValueError(f"Checkpoint has no 'state_dict': {weight_path}")
    state = OrderedDict(
        (key.removeprefix("module."), value) for key, value in checkpoint["state_dict"].items()
    )
    model.load_state_dict(state, strict=True)
    return model, config


def build_model_input(raw: np.ndarray, grid_size: float) -> dict:
    transform = Compose(
        [
            dict(
                type="GridSample", grid_size=grid_size, hash_type="fnv",
                mode="train", return_grid_coord=True, return_inverse=True,
            ),
            dict(type="ToTensor"),
            dict(type="Update", keys_dict={"grid_size": grid_size}),
            dict(
                type="Collect",
                keys=("coord", "grid_coord", "inverse", "grid_size"),
                feat_keys=("coord", "strength"),
            ),
        ]
    )
    return transform({"coord": raw[:, :3], "strength": raw[:, 3:4] / 255.0})


def write_labeled_ply(
    path: Path, coord: np.ndarray, semantic: np.ndarray,
    confidence: np.ndarray, source_index: np.ndarray,
) -> None:
    vertex = np.empty(
        coord.shape[0],
        dtype=[
            ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
            ("red", "u1"), ("green", "u1"), ("blue", "u1"),
            ("source_index", "<u4"), ("semantic_id", "<i4"), ("confidence", "<f4"),
        ],
    )
    vertex["x"], vertex["y"], vertex["z"] = coord.T
    rgb = COLORS[semantic]
    vertex["red"], vertex["green"], vertex["blue"] = rgb.T
    vertex["source_index"] = source_index
    vertex["semantic_id"] = semantic
    vertex["confidence"] = confidence
    temporary = _temporary_output_path(path)
    PlyData([PlyElement.describe(vertex, "vertex")], text=False).write(str(temporary))
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict:
    started = time.perf_counter()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for LitePT inference.")
    if not args.config.is_file():
        raise FileNotFoundError(f"Model config not found: {args.config}")
    if not args.weight.is_file():
        raise FileNotFoundError(f"Model checkpoint not found: {args.weight}")
    if args.grid_size <= 0:
        raise ValueError("--grid-size must be positive.")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    # Fully parse the source before an explicit overwrite can remove old artifacts.
    raw, source_index, invalid_count = load_nuscenes_bin(args.input, args.invalid_policy)
    prepare_output_dir(args.output_dir, args.overwrite_output)
    args._output_prepared = True
    write_json(
        args.output_dir / "request.json",
        {
            "schema_version": SCHEMA_VERSION,
            "task": "semantic_segmentation",
            "input": str(args.input.resolve()),
            "config": str(args.config.resolve()),
            "weight": str(args.weight.resolve()),
            "grid_size_m": args.grid_size,
            "seed": args.seed,
            "input_layout": "little-endian float32 N x 5: x,y,z,strength,ring_index",
            "invalid_policy": args.invalid_policy,
        },
    )
    emit_progress("validation", 0.05)
    point = build_model_input(raw, args.grid_size)
    sampled_count = int(point["coord"].shape[0])
    emit_progress("model", 0.15, points=int(raw.shape[0]), sampled_points=sampled_count)
    model, config = load_model(args.config, args.weight)
    for key, value in point.items():
        if isinstance(value, torch.Tensor):
            point[key] = value.cuda(non_blocking=True)

    torch.cuda.reset_peak_memory_stats()
    inference_started = time.perf_counter()
    emit_progress("inference", 0.3)
    with torch.no_grad():
        probabilities = torch.softmax(model(point)["seg_logits"], dim=1)
        sampled_confidence, sampled_semantic = probabilities.max(dim=1)
        inverse = point["inverse"].long()
        semantic = sampled_semantic[inverse].cpu().numpy().astype(np.int32)
        confidence = sampled_confidence[inverse].cpu().numpy().astype(np.float32)
    torch.cuda.synchronize()
    inference_seconds = time.perf_counter() - inference_started
    peak_memory = int(torch.cuda.max_memory_allocated())

    emit_progress("export", 0.9)
    original_count = int(source_index.shape[0] + invalid_count)
    full_labels = np.empty(original_count, dtype=LABEL_DTYPE)
    full_labels["semantic_id"] = -1
    full_labels["instance_id"] = -1
    full_labels["confidence"] = 0.0
    full_labels["semantic_id"][source_index] = semantic
    full_labels["confidence"][source_index] = confidence
    write_binary(args.output_dir / "point_labels.bin", full_labels)
    write_binary(args.output_dir / "source_index.bin", source_index.astype("<i8"))
    write_labeled_ply(args.output_dir / "labeled_cloud.ply", raw[:, :3], semantic, confidence, source_index)

    names = list(config.names)
    ids, counts = np.unique(semantic, return_counts=True)
    histogram = [
        {"semantic_id": int(label), "class_name": names[int(label)], "points": int(count)}
        for label, count in sorted(zip(ids, counts), key=lambda item: -item[1])
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "task": "semantic_segmentation",
        "input": str(args.input.resolve()),
        "config": str(args.config.resolve()),
        "weight": str(args.weight.resolve()),
        "input_points": original_count,
        "valid_points": int(raw.shape[0]),
        "invalid_points": invalid_count,
        "sampled_points": sampled_count,
        "seed": args.seed,
        "semantic_histogram": histogram,
        "inference_seconds": inference_seconds,
        "total_elapsed_seconds": time.perf_counter() - started,
        "peak_gpu_memory_bytes": peak_memory,
        "label_record": "little-endian int32 semantic_id, int32 instance_id=-1, float32 confidence; one record per original point",
        "source_index_record": "little-endian int64 original point index; one record per valid point",
    }
    write_json(args.output_dir / "run.json", result)
    emit_progress("complete", 1.0)
    return result


def main() -> None:
    args = parse_args()
    try:
        result = run(args)
    except Exception as exc:
        output_is_unused = not args.output_dir.exists() or not any(args.output_dir.iterdir())
        if (
            not isinstance(exc, FileExistsError)
            and (getattr(args, "_output_prepared", False) or output_is_unused)
        ):
            args.output_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                args.output_dir / "run.json",
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "error",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        emit_progress("error", 0.0, error_type=type(exc).__name__, error=str(exc))
        if args.debug:
            raise
        print(f"LitePT error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
