"""Validate a point cloud against a LitePT profile without using the GPU."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from infer_nuscenes_semantic import load_nuscenes_bin
from infer_scannet200_instances import detect_up_axis, load_cloud
from cloudforge_profiles import DEFAULT_PROFILE_FILE, get_profile


SCHEMA_VERSION = 1


def _bounds(coord: np.ndarray) -> tuple[list[float], list[float], list[float]]:
    lower = coord.min(axis=0)
    upper = coord.max(axis=0)
    return lower.astype(float).tolist(), upper.astype(float).tolist(), (upper - lower).astype(float).tolist()


def _scale_warnings(extent: np.ndarray, domain: str) -> list[str]:
    warnings = []
    largest = float(extent.max())
    if largest < 0.1:
        warnings.append(
            "Scene extent is below 0.1 m after unit conversion; check whether --input-unit is correct."
        )
    if largest > (1000.0 if "outdoor" in domain.lower() else 300.0):
        warnings.append(
            "Scene extent is unusually large in meters; check input units and corrupted coordinates."
        )
    if np.any(extent <= 1e-6):
        warnings.append("At least one coordinate axis has nearly zero extent.")
    return warnings


def inspect_input(
    input_path: Path,
    profile_name: str,
    profiles_file: Path = DEFAULT_PROFILE_FILE,
    input_unit: str = "m",
) -> dict:
    profile = get_profile(profile_name, profiles_file)
    suffix = input_path.suffix.lower().lstrip(".")
    formats = [str(value).lower().lstrip(".") for value in profile.input["formats"]]
    if suffix not in formats:
        raise ValueError(
            f"Profile '{profile.name}' accepts {formats}, but input is '{input_path.suffix or '<none>'}'."
        )
    if input_unit not in {"m", "cm", "mm"}:
        raise ValueError("input_unit must be one of: m, cm, mm.")
    unit_scale = {"m": 1.0, "cm": 0.01, "mm": 0.001}[input_unit]

    warnings: list[str] = []
    details: dict = {}
    if profile.task == "instance_segmentation":
        cloud = load_cloud(input_path, invalid_policy="drop")
        coord = cloud.coord.astype(np.float64) * unit_scale
        original_count = int(cloud.original_points)
        valid_count = int(cloud.coord.shape[0])
        invalid_count = int(cloud.invalid_points)
        color_span = np.ptp(cloud.color.astype(np.float64), axis=0)
        details = {
            "rgb": {
                "present": True,
                "min": cloud.color.min(axis=0).astype(float).tolist(),
                "max": cloud.color.max(axis=0).astype(float).tolist(),
                "channel_span": color_span.astype(float).tolist(),
            },
            "normals": {"present": cloud.normal is not None, "source": cloud.normal_source},
        }
        if float(color_span.max()) < 2.0:
            warnings.append("RGB values have almost no variation; this model expects real scene color.")
        model_grid_size = 0.02
        estimated_voxels = int(
            np.unique(
                np.floor(coord / model_grid_size).astype(np.int64), axis=0
            ).shape[0]
        )
        details["preprocessing"] = {
            "model_grid_size_m": model_grid_size,
            "estimated_sampled_points": estimated_voxels,
        }
        if estimated_voxels < 1000:
            warnings.append(
                "The 2 cm model grid leaves fewer than 1,000 estimated sampled points; "
                "instance proposals will be sparse or impossible (the network minimum is 200). "
                "This object scale does not match the released indoor-scene model."
            )
        if cloud.normal is not None:
            selected, scores = detect_up_axis(cloud)
            details["coordinate_system"] = {
                "model_expected_up_axis": "z",
                "detected_input_up_axis": selected,
                "scores": scores,
            }
        else:
            details["coordinate_system"] = {
                "model_expected_up_axis": "z",
                "detected_input_up_axis": None,
                "note": "Normals are absent; inference estimates them before automatic up-axis detection.",
            }
    elif profile.task == "semantic_segmentation":
        if input_unit != "m":
            raise ValueError("NuScenes .bin profiles require meter coordinates; --input-unit must be m.")
        raw, _source_index, invalid_count = load_nuscenes_bin(input_path, invalid_policy="drop")
        coord = raw[:, :3].astype(np.float64)
        original_count = int(raw.shape[0] + invalid_count)
        valid_count = int(raw.shape[0])
        details = {
            "strength": {
                "min": float(raw[:, 3].min()),
                "max": float(raw[:, 3].max()),
                "normalization": "value / 255",
            },
            "ring_index": {
                "min": float(raw[:, 4].min()),
                "max": float(raw[:, 4].max()),
                "used_by_model": False,
            },
            "coordinate_system": {
                "model_expected_up_axis": "z",
                "detected_input_up_axis": None,
                "note": "NuScenes-format sweeps are assumed to use the dataset coordinate convention.",
            },
        }
        if float(raw[:, 3].max()) > 255.0 or float(raw[:, 3].min()) < 0.0:
            warnings.append("Strength values fall outside the expected 0..255 range.")
    else:
        raise ValueError(f"Unsupported profile task: {profile.task}")

    lower, upper, extent = _bounds(coord)
    warnings.extend(_scale_warnings(np.asarray(extent), profile.domain))
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "valid_with_warnings" if warnings else "valid",
        "profile": profile.name,
        "task": profile.task,
        "domain": profile.domain,
        "input": str(input_path.resolve()),
        "format": suffix,
        "input_unit": input_unit,
        "original_points": original_count,
        "valid_points": valid_count,
        "invalid_points": invalid_count,
        "bounds_m": {"min": lower, "max": upper, "extent": extent},
        **details,
        "warnings": warnings,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--profile", default="scannet200-instances")
    parser.add_argument("--profiles-file", type=Path, default=DEFAULT_PROFILE_FILE)
    parser.add_argument("--input-unit", choices=("m", "cm", "mm"), default="m")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(
        json.dumps(
            inspect_input(args.input, args.profile, args.profiles_file, args.input_unit),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"LitePT input error: {exc}", file=sys.stderr)
        raise SystemExit(2)
