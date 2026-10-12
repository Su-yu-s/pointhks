"""Measure repeatable end-to-end LitePT inference latency and GPU memory."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def percentile(values: list[float], value: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot calculate a percentile of an empty list.")
    position = (len(ordered) - 1) * value
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--profile", default="scannet200-instances")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1)
    args, forwarded = parser.parse_known_args()
    if args.repeat < 1 or args.warmup < 0:
        raise ValueError("--repeat must be positive and --warmup must be non-negative.")
    args.output_root.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve().parent / "cloudforge_infer.py"
    measurements = []
    for index in range(args.warmup + args.repeat):
        phase = "warmup" if index < args.warmup else "measurement"
        phase_index = index if phase == "warmup" else index - args.warmup
        output_dir = args.output_root / f"{phase}_{phase_index:02d}"
        command = [
            sys.executable, str(script), str(args.input), "--profile", args.profile,
            "--output-dir", str(output_dir), *forwarded,
        ]
        wall_started = time.perf_counter()
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        wall_seconds = time.perf_counter() - wall_started
        if completed.returncode:
            raise RuntimeError(
                f"Benchmark run {index} failed with code {completed.returncode}:\n{completed.stderr[-4000:]}"
            )
        run = json.loads((output_dir / "run.json").read_text(encoding="utf-8"))
        if phase == "measurement":
            measurements.append(
                {
                    "index": phase_index,
                    "wall_seconds": wall_seconds,
                    "inference_seconds": float(run.get("inference_seconds", run.get("elapsed_seconds"))),
                    "pipeline_seconds": float(run["total_elapsed_seconds"]),
                    "peak_gpu_memory_bytes": int(run["peak_gpu_memory_bytes"]),
                    "input_points": int(run["input_points"]),
                }
            )
    pipeline = [value["pipeline_seconds"] for value in measurements]
    inference = [value["inference_seconds"] for value in measurements]
    wall = [value["wall_seconds"] for value in measurements]
    points = measurements[0]["input_points"]
    result = {
        "schema_version": 1,
        "profile": args.profile,
        "input": str(args.input.resolve()),
        "repeat": args.repeat,
        "warmup": args.warmup,
        "pipeline_seconds": {
            "median": percentile(pipeline, 0.5),
            "p95": percentile(pipeline, 0.95),
        },
        "wall_seconds": {
            "median": percentile(wall, 0.5),
            "p95": percentile(wall, 0.95),
        },
        "inference_seconds": {
            "median": percentile(inference, 0.5),
            "p95": percentile(inference, 0.95),
        },
        "throughput_points_per_second": points / percentile(pipeline, 0.5),
        "peak_gpu_memory_bytes": max(value["peak_gpu_memory_bytes"] for value in measurements),
        "measurements": measurements,
    }
    report = args.output_root / "benchmark.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
