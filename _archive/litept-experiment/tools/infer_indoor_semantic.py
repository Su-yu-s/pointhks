"""Run LitePT indoor semantic segmentation on an RGB PLY or PCD cloud."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.infer_scannet200_instances import (
    LABEL_DTYPE,
    Cloud,
    apply_up_axis,
    build_adaptive_xy_tiles,
    detect_up_axis,
    emit_progress,
    estimate_normals,
    is_cuda_out_of_memory,
    load_cloud,
    load_model,
    prepare_input,
    prepare_output_dir,
    write_binary_array,
    write_json,
    write_labeled_ply,
)


SCHEMA_VERSION = 1
DEFAULT_CONFIG = ROOT / "configs" / "scannet" / "semseg-litept-small-v1m1.py"
DEFAULT_WEIGHT = ROOT / "weights" / "scannet-semseg-litept-small-v1m1" / "model_best.pth"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input RGB .ply or .pcd point cloud.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "outputs" / "indoor_semantic",
    )
    parser.add_argument("--weight", type=Path, default=DEFAULT_WEIGHT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--overwrite-output", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--input-unit", choices=("m", "cm", "mm"), default="m")
    parser.add_argument("--grid-size", type=float, default=0.02)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--normal-k", type=int, default=30)
    parser.add_argument(
        "--up-axis",
        choices=("auto", "z", "-z", "y", "-y", "x", "-x"),
        default="auto",
    )
    parser.add_argument("--invalid-policy", choices=("drop", "error"), default="drop")
    parser.add_argument("--tiling", choices=("auto", "off", "on"), default="auto")
    parser.add_argument("--tile-threshold", type=int, default=500_000)
    parser.add_argument("--tile-size", type=float, default=6.0)
    parser.add_argument("--tile-overlap", type=float, default=0.75)
    parser.add_argument("--max-tile-points", type=int, default=750_000)
    parser.add_argument("--min-tile-size", type=float, default=1.0)
    return parser.parse_args()


def infer_once(
    model: torch.nn.Module,
    cloud: Cloud,
    grid_size: float,
) -> tuple[np.ndarray, np.ndarray, int, float, int]:
    model_input = prepare_input(cloud, grid_size)
    inverse = model_input["inverse"].numpy().astype(np.int64)
    sampled_points = int(model_input["coord"].shape[0])
    for key, value in model_input.items():
        if isinstance(value, torch.Tensor):
            model_input[key] = value.cuda(non_blocking=True)

    torch.cuda.reset_peak_memory_stats()
    torch.cuda.synchronize()
    started = time.perf_counter()
    with torch.no_grad():
        logits = model(model_input)["seg_logits"]
        probabilities = torch.softmax(logits, dim=1)
        sampled_confidence, sampled_semantic = probabilities.max(dim=1)
        semantic = sampled_semantic[inverse].cpu().numpy().astype(np.int32)
        confidence = sampled_confidence[inverse].cpu().numpy().astype(np.float32)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    peak_memory = int(torch.cuda.max_memory_allocated())
    return semantic, confidence, sampled_points, elapsed, peak_memory


def infer_tiled(
    model: torch.nn.Module,
    cloud: Cloud,
    args: argparse.Namespace,
) -> tuple[np.ndarray, np.ndarray, int, float, int, int, float, list[dict]]:
    tiles, actual_size, attempts = build_adaptive_xy_tiles(
        cloud.coord,
        args.tile_size,
        args.tile_overlap,
        args.max_tile_points,
        args.min_tile_size,
    )
    semantic = np.full(cloud.coord.shape[0], -1, dtype=np.int32)
    confidence = np.full(cloud.coord.shape[0], -np.inf, dtype=np.float32)
    sampled_points = 0
    elapsed = 0.0
    peak_memory = 0

    for number, tile in enumerate(tiles, start=1):
        emit_progress(
            "inference",
            0.2 + 0.65 * (number - 1) / max(1, len(tiles)),
            tile=number,
            tiles=len(tiles),
            tile_points=int(tile.indices.shape[0]),
        )
        tile_cloud = Cloud(
            coord=cloud.coord[tile.indices],
            color=cloud.color[tile.indices],
            normal=cloud.normal[tile.indices],
        )
        tile_semantic, tile_confidence, tile_sampled, tile_elapsed, tile_peak = infer_once(
            model,
            tile_cloud,
            args.grid_size,
        )
        better = tile_confidence > confidence[tile.indices]
        destination = tile.indices[better]
        semantic[destination] = tile_semantic[better]
        confidence[destination] = tile_confidence[better]
        sampled_points += tile_sampled
        elapsed += tile_elapsed
        peak_memory = max(peak_memory, tile_peak)

    if np.any(semantic < 0):
        raise RuntimeError("Tiled inference left points without a semantic prediction.")
    return (
        semantic,
        confidence,
        sampled_points,
        elapsed,
        peak_memory,
        len(tiles),
        actual_size,
        attempts,
    )


def run(args: argparse.Namespace) -> dict:
    total_started = time.perf_counter()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required.")
    if not args.input.is_file():
        raise FileNotFoundError(args.input)
    if not args.weight.is_file():
        raise FileNotFoundError(args.weight)
    if not args.config.is_file():
        raise FileNotFoundError(args.config)
    if args.grid_size <= 0 or args.normal_k < 3:
        raise ValueError("--grid-size must be positive and --normal-k must be at least 3.")
    if args.tile_size <= 0 or args.tile_overlap < 0 or args.tile_overlap >= args.tile_size:
        raise ValueError("Tile size must be positive and overlap must be smaller than tile size.")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    cloud = load_cloud(args.input, args.invalid_policy)
    prepare_output_dir(args.output_dir, args.overwrite_output)
    args._output_prepared = True
    unit_scale = {"m": 1.0, "cm": 0.01, "mm": 0.001}[args.input_unit]
    cloud.coord = cloud.coord.astype(np.float32) * unit_scale

    emit_progress("normals", 0.05, points=int(cloud.coord.shape[0]))
    normal_source = cloud.normal_source or "estimated_knn"
    if cloud.normal is None:
        cloud.normal = estimate_normals(cloud.coord, args.normal_k)
    else:
        length = np.linalg.norm(cloud.normal, axis=1, keepdims=True)
        cloud.normal = cloud.normal / np.maximum(length, 1e-8)

    if args.up_axis == "auto":
        selected_up_axis, up_axis_scores = detect_up_axis(cloud)
    else:
        selected_up_axis, up_axis_scores = args.up_axis, {}
    apply_up_axis(cloud, selected_up_axis)
    emit_progress("orientation", 0.12, selected_up_axis=selected_up_axis)

    request = {
        "schema_version": SCHEMA_VERSION,
        "task": "semantic_segmentation",
        "input": str(args.input.resolve()),
        "weight": str(args.weight.resolve()),
        "config": str(args.config.resolve()),
        "input_unit": args.input_unit,
        "grid_size_m": args.grid_size,
        "normal_k": args.normal_k,
        "up_axis": args.up_axis,
        "invalid_policy": args.invalid_policy,
        "tiling": args.tiling,
    }
    write_json(args.output_dir / "request.json", request)

    emit_progress("model", 0.16)
    model, config = load_model(args.config, args.weight)
    tiling_enabled = args.tiling == "on" or (
        args.tiling == "auto" and cloud.coord.shape[0] > args.tile_threshold
    )
    tile_count = 1
    actual_tile_size = None
    tile_attempts: list[dict] = []
    if tiling_enabled:
        (
            semantic,
            confidence,
            sampled_points,
            inference_seconds,
            peak_memory,
            tile_count,
            actual_tile_size,
            tile_attempts,
        ) = infer_tiled(model, cloud, args)
    else:
        emit_progress("inference", 0.25, tiles=1)
        try:
            semantic, confidence, sampled_points, inference_seconds, peak_memory = infer_once(
                model,
                cloud,
                args.grid_size,
            )
        except Exception as exc:
            if is_cuda_out_of_memory(exc):
                raise RuntimeError(
                    "CUDA ran out of memory. Rerun with --tiling on or reduce --tile-size."
                ) from exc
            raise

    labels = np.empty(cloud.coord.shape[0], dtype=LABEL_DTYPE)
    labels["semantic_id"] = semantic
    labels["instance_id"] = -1
    labels["confidence"] = confidence
    full_labels = np.empty(cloud.original_points, dtype=LABEL_DTYPE)
    full_labels["semantic_id"] = -1
    full_labels["instance_id"] = -1
    full_labels["confidence"] = 0.0
    full_labels[cloud.source_index] = labels

    emit_progress("export", 0.9)
    write_binary_array(args.output_dir / "point_labels.bin", full_labels)
    write_binary_array(args.output_dir / "source_index.bin", cloud.source_index.astype("<i8"))
    write_labeled_ply(
        args.output_dir / "labeled_cloud.ply",
        cloud.coord,
        labels,
        cloud.source_index,
    )

    names = list(config.data.names)
    ids, counts = np.unique(semantic, return_counts=True)
    histogram = [
        {
            "semantic_id": int(label),
            "class_name": names[int(label)] if 0 <= int(label) < len(names) else "unknown",
            "points": int(count),
        }
        for label, count in sorted(zip(ids, counts), key=lambda item: -item[1])
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "task": "semantic_segmentation",
        "input": str(args.input.resolve()),
        "config": str(args.config.resolve()),
        "weight": str(args.weight.resolve()),
        "input_points": int(cloud.original_points),
        "valid_points": int(cloud.coord.shape[0]),
        "invalid_points": int(cloud.invalid_points),
        "sampled_points": sampled_points,
        "normal_source": normal_source,
        "selected_up_axis": selected_up_axis,
        "up_axis_scores": up_axis_scores,
        "semantic_histogram": histogram,
        "tiling": {
            "enabled": tiling_enabled,
            "tile_count": tile_count,
            "actual_tile_size_m": actual_tile_size,
            "attempts": tile_attempts,
        },
        "inference_seconds": inference_seconds,
        "total_elapsed_seconds": time.perf_counter() - total_started,
        "peak_gpu_memory_bytes": peak_memory,
        "label_record": (
            "little-endian int32 semantic_id, int32 instance_id=-1, "
            "float32 confidence; one record per original point"
        ),
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
