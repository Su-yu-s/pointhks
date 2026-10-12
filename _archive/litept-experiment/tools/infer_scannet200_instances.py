"""Run LitePT ScanNet200 instance segmentation on an RGB PLY or PCD cloud."""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import random
import shutil
import struct
import sys
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import torch
from plyfile import PlyData, PlyElement
from scipy.spatial import cKDTree

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datasets.transform import Compose
from models import build_model
from utils.config import Config


DEFAULT_CONFIG = ROOT / "configs" / "scannet200" / "insseg-litept-small-v1m2.py"
DEFAULT_WEIGHT = (
    ROOT
    / "weights"
    / "scannet200-insseg-litept-small-v1m2"
    / "model_best.pth"
)
SCHEMA_VERSION = 1
LABEL_DTYPE = np.dtype(
    [("semantic_id", "<i4"), ("instance_id", "<i4"), ("confidence", "<f4")]
)


@dataclass
class Cloud:
    coord: np.ndarray
    color: np.ndarray
    normal: np.ndarray | None
    normal_source: str | None = None
    source_index: np.ndarray | None = None
    original_points: int | None = None
    invalid_points: int = 0


@dataclass(frozen=True)
class SpatialTile:
    tile_id: int
    grid_x: int
    grid_y: int
    indices: np.ndarray


@dataclass
class TilePrediction:
    tile: SpatialTile
    labels: np.ndarray
    semantic_confidence: np.ndarray
    instances: list[dict]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Input .ply or .pcd with XYZ and RGB.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs" / "scannet200_instances")
    parser.add_argument("--weight", type=Path, default=DEFAULT_WEIGHT)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--overwrite-output",
        action="store_true",
        help="Replace known LitePT artifacts in a non-empty output directory.",
    )
    parser.add_argument("--debug", action="store_true", help="Show a Python traceback on failure.")
    parser.add_argument("--input-unit", choices=("m", "cm", "mm"), default="m")
    parser.add_argument("--grid-size", type=float, default=0.02)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--normal-k", type=int, default=30)
    parser.add_argument("--min-confidence", type=float, default=0.0)
    parser.add_argument(
        "--tiling",
        choices=("auto", "off", "on"),
        default="auto",
        help="Split large scenes into overlapping XY tiles. Auto enables it above --tile-threshold points.",
    )
    parser.add_argument("--tile-threshold", type=int, default=500_000)
    parser.add_argument("--tile-size", type=float, default=6.0, help="Tile core width in meters.")
    parser.add_argument("--tile-overlap", type=float, default=0.75, help="Tile halo width in meters.")
    parser.add_argument(
        "--max-tile-points",
        type=int,
        default=750_000,
        help="Halve tile size until every halo tile is at or below this raw point count.",
    )
    parser.add_argument(
        "--min-tile-size",
        type=float,
        default=1.0,
        help="Smallest tile core width allowed during adaptive subdivision.",
    )
    parser.add_argument("--oom-retries", type=int, default=2)
    parser.add_argument("--tile-merge-ratio", type=float, default=0.5)
    parser.add_argument("--tile-merge-iou", type=float, default=0.25)
    parser.add_argument("--tile-min-shared-points", type=int, default=20)
    parser.add_argument("--min-instance-points", type=int, default=50)
    parser.add_argument(
        "--postprocess",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Clean, split, and conservatively merge predicted instances.",
    )
    parser.add_argument("--component-voxel-size", type=float, default=0.08)
    parser.add_argument("--component-min-points", type=int, default=20)
    parser.add_argument("--component-min-ratio", type=float, default=0.01)
    parser.add_argument(
        "--instance-merge-distance",
        type=float,
        default=0.04,
        help="Maximum point-to-point distance for merging same-class instances; 0 disables merging.",
    )
    parser.add_argument(
        "--up-axis",
        choices=("auto", "z", "-z", "y", "-y", "x", "-x"),
        default="auto",
        help="Input up direction. 'auto' detects X/Y/Z and converts it to Z-up.",
    )
    parser.add_argument(
        "--invalid-policy",
        choices=("drop", "error"),
        default="drop",
        help="Drop NaN/Inf points while preserving source indices, or fail.",
    )
    parser.add_argument(
        "--export-instances",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Write every accepted instance to instances/instance_NNN_class.ply.",
    )
    return parser.parse_args()


class _DisjointSet:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def build_xy_tiles(coord: np.ndarray, tile_size: float, overlap: float) -> list[SpatialTile]:
    """Partition Z-up points into deterministic XY core tiles with an overlap halo."""
    if coord.ndim != 2 or coord.shape[1] != 3:
        raise ValueError("coord must have shape N x 3.")
    if coord.shape[0] == 0:
        return []
    if tile_size <= 0:
        raise ValueError("tile_size must be positive.")
    if overlap < 0 or overlap >= tile_size:
        raise ValueError("tile overlap must be non-negative and smaller than tile size.")

    origin = coord[:, :2].min(axis=0).astype(np.float64)
    grid = np.floor((coord[:, :2] - origin) / tile_size).astype(np.int64)
    bins: dict[tuple[int, int], list[int]] = {}
    for point_index, key_values in enumerate(grid):
        key = (int(key_values[0]), int(key_values[1]))
        bins.setdefault(key, []).append(point_index)

    tiles: list[SpatialTile] = []
    for tile_id, (grid_x, grid_y) in enumerate(sorted(bins)):
        candidates: list[np.ndarray] = []
        for neighbor_x in range(grid_x - 1, grid_x + 2):
            for neighbor_y in range(grid_y - 1, grid_y + 2):
                values = bins.get((neighbor_x, neighbor_y))
                if values:
                    candidates.append(np.asarray(values, dtype=np.int64))
        candidate = np.concatenate(candidates)
        lower = origin + np.array([grid_x, grid_y], dtype=np.float64) * tile_size - overlap
        upper = lower + tile_size + 2.0 * overlap
        xy = coord[candidate, :2]
        epsilon = max(tile_size, 1.0) * 1e-6
        inside = (
            (xy[:, 0] >= lower[0] - epsilon)
            & (xy[:, 0] <= upper[0] + epsilon)
            & (xy[:, 1] >= lower[1] - epsilon)
            & (xy[:, 1] <= upper[1] + epsilon)
        )
        indices = np.sort(candidate[inside])
        tiles.append(SpatialTile(tile_id, grid_x, grid_y, indices))
    return tiles


def build_adaptive_xy_tiles(
    coord: np.ndarray,
    tile_size: float,
    overlap: float,
    max_tile_points: int,
    min_tile_size: float,
) -> tuple[list[SpatialTile], float, list[dict]]:
    """Uniformly halve tile size until every halo tile satisfies the point limit."""
    if max_tile_points < 1:
        raise ValueError("max_tile_points must be positive.")
    if min_tile_size <= overlap:
        raise ValueError("min_tile_size must be greater than tile overlap.")
    if min_tile_size > tile_size:
        raise ValueError("min_tile_size must not exceed tile_size.")

    actual_size = float(tile_size)
    attempts: list[dict] = []
    while True:
        tiles = build_xy_tiles(coord, actual_size, overlap)
        largest = max((int(tile.indices.shape[0]) for tile in tiles), default=0)
        attempts.append(
            {
                "tile_size_m": actual_size,
                "tile_count": len(tiles),
                "largest_tile_points": largest,
            }
        )
        if largest <= max_tile_points:
            return tiles, actual_size, attempts
        next_size = max(actual_size * 0.5, min_tile_size)
        if next_size >= actual_size or next_size <= overlap:
            raise ValueError(
                "A spatial tile still contains "
                f"{largest} points at {actual_size:g} m, but it cannot be split below "
                f"--min-tile-size {min_tile_size:g} m with overlap {overlap:g} m. "
                "Increase --max-tile-points, reduce --tile-overlap, or reduce "
                "--min-tile-size."
            )
        actual_size = next_size


def _neighboring_tiles(left: SpatialTile, right: SpatialTile) -> bool:
    return abs(left.grid_x - right.grid_x) <= 1 and abs(left.grid_y - right.grid_y) <= 1


def fuse_tiled_predictions(
    point_count: int,
    predictions: list[TilePrediction],
    min_instance_points: int = 50,
    merge_ratio: float = 0.5,
    merge_iou: float = 0.25,
    min_shared_points: int = 20,
) -> tuple[np.ndarray, list[dict]]:
    """Fuse overlapping tile predictions back onto the valid full-resolution cloud."""
    if point_count < 0:
        raise ValueError("point_count must be non-negative.")
    if min_instance_points < 1 or min_shared_points < 1:
        raise ValueError("instance point thresholds must be positive.")
    if not 0.0 <= merge_ratio <= 1.0 or not 0.0 <= merge_iou <= 1.0:
        raise ValueError("tile merge thresholds must be between 0 and 1.")

    semantic = np.full(point_count, -1, dtype=np.int32)
    semantic_confidence = np.full(point_count, -np.inf, dtype=np.float32)
    records: list[dict] = []
    prediction_record_ids: list[dict[int, int]] = []

    for prediction in predictions:
        if prediction.labels.shape[0] != prediction.tile.indices.shape[0]:
            raise ValueError("Tile label count does not match tile point count.")
        if prediction.semantic_confidence.shape[0] != prediction.tile.indices.shape[0]:
            raise ValueError("Tile semantic confidence count does not match tile point count.")
        indices = prediction.tile.indices
        better = prediction.semantic_confidence > semantic_confidence[indices]
        semantic[indices[better]] = prediction.labels["semantic_id"][better]
        semantic_confidence[indices[better]] = prediction.semantic_confidence[better]

        local_to_record: dict[int, int] = {}
        for instance in prediction.instances:
            local_id = int(instance["instance_id"])
            mask = prediction.labels["instance_id"] == local_id
            if not mask.any():
                continue
            record_id = len(records)
            local_to_record[local_id] = record_id
            records.append(
                {
                    "semantic_id": int(instance["semantic_id"]),
                    "score": float(instance["score"]),
                    "points": indices[mask],
                }
            )
        prediction_record_ids.append(local_to_record)

    disjoint = _DisjointSet(len(records))
    for left_index, left in enumerate(predictions):
        for right_index in range(left_index + 1, len(predictions)):
            right = predictions[right_index]
            if not _neighboring_tiles(left.tile, right.tile):
                continue
            _, left_pos, right_pos = np.intersect1d(
                left.tile.indices,
                right.tile.indices,
                assume_unique=True,
                return_indices=True,
            )
            if left_pos.size == 0:
                continue
            left_ids = left.labels["instance_id"][left_pos]
            right_ids = right.labels["instance_id"][right_pos]
            valid = (left_ids >= 0) & (right_ids >= 0)
            if not valid.any():
                continue

            left_support_ids, left_support_counts = np.unique(left_ids[left_ids >= 0], return_counts=True)
            right_support_ids, right_support_counts = np.unique(right_ids[right_ids >= 0], return_counts=True)
            left_support = dict(zip(left_support_ids.tolist(), left_support_counts.tolist()))
            right_support = dict(zip(right_support_ids.tolist(), right_support_counts.tolist()))
            pairs = np.column_stack((left_ids[valid], right_ids[valid]))
            unique_pairs, shared_counts = np.unique(pairs, axis=0, return_counts=True)
            for pair, shared_value in zip(unique_pairs, shared_counts):
                shared = int(shared_value)
                if shared < min_shared_points:
                    continue
                left_local, right_local = int(pair[0]), int(pair[1])
                left_record = prediction_record_ids[left_index].get(left_local)
                right_record = prediction_record_ids[right_index].get(right_local)
                if left_record is None or right_record is None:
                    continue
                if records[left_record]["semantic_id"] != records[right_record]["semantic_id"]:
                    continue
                left_count = int(left_support[left_local])
                right_count = int(right_support[right_local])
                ratio = shared / max(1, min(left_count, right_count))
                iou = shared / max(1, left_count + right_count - shared)
                if ratio >= merge_ratio or iou >= merge_iou:
                    disjoint.union(left_record, right_record)

    point_root = np.full(point_count, -1, dtype=np.int64)
    point_instance_confidence = np.full(point_count, -np.inf, dtype=np.float32)
    for record_id in sorted(range(len(records)), key=lambda index: -records[index]["score"]):
        record = records[record_id]
        indices = record["points"]
        score = float(record["score"])
        better = score > point_instance_confidence[indices]
        point_root[indices[better]] = disjoint.find(record_id)
        point_instance_confidence[indices[better]] = score

    roots, counts = np.unique(point_root[point_root >= 0], return_counts=True)
    retained = [int(root) for root, count in zip(roots, counts) if int(count) >= min_instance_points]
    retained.sort(
        key=lambda root: (
            -max(records[index]["score"] for index in range(len(records)) if disjoint.find(index) == root),
            min(records[index]["semantic_id"] for index in range(len(records)) if disjoint.find(index) == root),
            root,
        )
    )

    labels = np.empty(point_count, dtype=LABEL_DTYPE)
    labels["semantic_id"] = semantic
    labels["instance_id"] = -1
    labels["confidence"] = np.maximum(semantic_confidence, 0.0)
    accepted: list[dict] = []
    for instance_id, root in enumerate(retained):
        mask = point_root == root
        member_ids = [index for index in range(len(records)) if disjoint.find(index) == root]
        semantic_id = records[member_ids[0]]["semantic_id"]
        score = max(float(records[index]["score"]) for index in member_ids)
        labels["instance_id"][mask] = instance_id
        labels["confidence"][mask] = point_instance_confidence[mask]
        accepted.append(
            {
                "instance_id": instance_id,
                "semantic_id": semantic_id,
                "score": score,
                "sampled_points": int(mask.sum()),
            }
        )
    return labels, accepted


def _stack_fields(array: np.ndarray, names: tuple[str, ...]) -> np.ndarray:
    return np.column_stack([np.asarray(array[name]) for name in names]).astype(np.float32)


def _decode_packed_rgb(values: np.ndarray, value_type: str = "F") -> np.ndarray:
    values = np.asarray(values)
    if value_type.upper() == "F":
        packed = values.astype("<f4", copy=False).view("<u4")
    else:
        packed = values.astype("<u4", copy=False)
    return np.column_stack(
        ((packed >> 16) & 255, (packed >> 8) & 255, packed & 255)
    ).astype(np.float32)


def mesh_vertex_normals(coord: np.ndarray, faces: np.ndarray) -> np.ndarray:
    triangles: list[tuple[int, int, int]] = []
    for face in faces:
        indices = np.asarray(face, dtype=np.int64)
        if indices.size < 3:
            continue
        triangles.extend(
            (int(indices[0]), int(indices[index]), int(indices[index + 1]))
            for index in range(1, indices.size - 1)
        )
    if not triangles:
        raise ValueError("PLY face data contains no valid triangles.")
    triangle = np.asarray(triangles, dtype=np.int64)
    cross = np.cross(
        coord[triangle[:, 1]] - coord[triangle[:, 0]],
        coord[triangle[:, 2]] - coord[triangle[:, 0]],
    )
    normal = np.zeros_like(coord, dtype=np.float32)
    np.add.at(normal, triangle[:, 0], cross)
    np.add.at(normal, triangle[:, 1], cross)
    np.add.at(normal, triangle[:, 2], cross)
    length = np.linalg.norm(normal, axis=1, keepdims=True)
    return normal / np.maximum(length, 1e-8)


def load_ply(path: Path) -> Cloud:
    ply = PlyData.read(str(path))
    if "vertex" not in ply:
        raise ValueError("PLY does not contain a vertex element.")
    vertex = ply["vertex"].data
    names = set(vertex.dtype.names or ())
    if not {"x", "y", "z"}.issubset(names):
        raise ValueError("PLY must contain x, y and z fields.")
    coord = _stack_fields(vertex, ("x", "y", "z"))

    if {"red", "green", "blue"}.issubset(names):
        color = _stack_fields(vertex, ("red", "green", "blue"))
    elif {"r", "g", "b"}.issubset(names):
        color = _stack_fields(vertex, ("r", "g", "b"))
    elif "rgb" in names:
        value_type = "F" if vertex.dtype["rgb"].kind == "f" else "U"
        color = _decode_packed_rgb(vertex["rgb"], value_type)
    else:
        raise ValueError(
            "PLY has no RGB fields. ScanNet200 inference requires real color data."
        )

    normal = None
    normal_source = None
    if {"nx", "ny", "nz"}.issubset(names):
        normal = _stack_fields(vertex, ("nx", "ny", "nz"))
        normal_source = "input"
    elif {"normal_x", "normal_y", "normal_z"}.issubset(names):
        normal = _stack_fields(vertex, ("normal_x", "normal_y", "normal_z"))
        normal_source = "input"
    elif "face" in ply and "vertex_indices" in (ply["face"].data.dtype.names or ()):
        normal = mesh_vertex_normals(coord, ply["face"].data["vertex_indices"])
        normal_source = "mesh_faces"
    return Cloud(
        coord,
        color,
        normal,
        normal_source,
        np.arange(coord.shape[0], dtype=np.int64),
        int(coord.shape[0]),
    )


def _pcd_dtype(fields: list[str], sizes: list[int], types: list[str]) -> np.dtype:
    type_map = {
        ("F", 4): "<f4",
        ("F", 8): "<f8",
        ("I", 1): "i1",
        ("I", 2): "<i2",
        ("I", 4): "<i4",
        ("I", 8): "<i8",
        ("U", 1): "u1",
        ("U", 2): "<u2",
        ("U", 4): "<u4",
        ("U", 8): "<u8",
    }
    try:
        return np.dtype([(name, type_map[(kind.upper(), size)]) for name, size, kind in zip(fields, sizes, types)])
    except KeyError as exc:
        raise ValueError(f"Unsupported PCD field type: {exc.args[0]}") from exc


def _pcd_scalar_dtype(size: int, kind: str) -> np.dtype:
    return _pcd_dtype(["value"], [size], [kind]).fields["value"][0]


def _lzf_decompress(payload: bytes, expected_size: int) -> bytes:
    """Decode the LZF byte stream used by PCD DATA binary_compressed."""
    output = bytearray()
    cursor = 0
    while cursor < len(payload):
        control = payload[cursor]
        cursor += 1
        if control < 32:
            length = control + 1
            stop = cursor + length
            if stop > len(payload):
                raise ValueError("PCD LZF literal run exceeds compressed payload.")
            output.extend(payload[cursor:stop])
            cursor = stop
        else:
            length = control >> 5
            reference_offset = (control & 0x1F) << 8
            if length == 7:
                if cursor >= len(payload):
                    raise ValueError("PCD LZF length extension is truncated.")
                length += payload[cursor]
                cursor += 1
            if cursor >= len(payload):
                raise ValueError("PCD LZF back-reference is truncated.")
            reference_offset += payload[cursor]
            cursor += 1
            reference = len(output) - reference_offset - 1
            length += 2
            if reference < 0:
                raise ValueError("PCD LZF back-reference points before output start.")
            for _ in range(length):
                if reference >= len(output):
                    raise ValueError("PCD LZF back-reference exceeds decoded output.")
                output.append(output[reference])
                reference += 1
        if len(output) > expected_size:
            raise ValueError("PCD LZF output exceeds declared uncompressed size.")
    if len(output) != expected_size:
        raise ValueError(
            f"PCD LZF size mismatch: decoded {len(output)} bytes, expected {expected_size}."
        )
    return bytes(output)


def _decode_binary_compressed_pcd(
    payload: bytes,
    fields: list[str],
    sizes: list[int],
    types: list[str],
    counts: list[int],
    points: int,
) -> dict[str, np.ndarray]:
    if len(payload) < 8:
        raise ValueError("PCD binary_compressed payload is missing its size header.")
    compressed_size, uncompressed_size = struct.unpack_from("<II", payload, 0)
    if len(payload) - 8 < compressed_size:
        raise ValueError(
            "PCD binary_compressed payload is truncated: "
            f"{len(payload) - 8} < {compressed_size} bytes."
        )
    expected_size = points * sum(size * count for size, count in zip(sizes, counts))
    if uncompressed_size != expected_size:
        raise ValueError(
            "PCD binary_compressed uncompressed size does not match POINTS and fields: "
            f"{uncompressed_size} != {expected_size}."
        )
    decoded = _lzf_decompress(payload[8 : 8 + compressed_size], uncompressed_size)
    values: dict[str, np.ndarray] = {}
    offset = 0
    for field, size, kind, count in zip(fields, sizes, types, counts):
        block_size = points * size * count
        block = decoded[offset : offset + block_size]
        dtype = _pcd_scalar_dtype(size, kind)
        array = np.frombuffer(block, dtype=dtype, count=points * count)
        values[field] = array.reshape(points, count) if count > 1 else array
        offset += block_size
    return values


def load_pcd(path: Path) -> Cloud:
    header: dict[str, list[str]] = {}
    with path.open("rb") as stream:
        while True:
            line = stream.readline()
            if not line:
                raise ValueError("PCD header is incomplete: DATA line not found.")
            text = line.decode("ascii", errors="strict").strip()
            if not text or text.startswith("#"):
                continue
            parts = text.split()
            header[parts[0].upper()] = parts[1:]
            if parts[0].upper() == "DATA":
                payload = stream.read()
                break

    fields = [name.lower() for name in header.get("FIELDS", [])]
    if not fields:
        raise ValueError("PCD header has no FIELDS entry.")
    sizes = [int(value) for value in header.get("SIZE", [])]
    types = header.get("TYPE", [])
    counts = [int(value) for value in header.get("COUNT", ["1"] * len(fields))]
    if not (len(fields) == len(sizes) == len(types) == len(counts)):
        raise ValueError("PCD FIELDS/SIZE/TYPE/COUNT lengths do not match.")
    if any(count != 1 for count in counts):
        raise ValueError("PCD fields with COUNT other than 1 are not supported.")
    points = int(header.get("POINTS", header.get("WIDTH", ["0"]))[0])
    mode = header["DATA"][0].lower()

    if mode == "binary":
        dtype = _pcd_dtype(fields, sizes, types)
        expected = points * dtype.itemsize
        if len(payload) < expected:
            raise ValueError(f"PCD binary payload is truncated: {len(payload)} < {expected} bytes.")
        data = np.frombuffer(payload[:expected], dtype=dtype, count=points)
        values = {field: np.asarray(data[field]) for field in fields}
    elif mode == "ascii":
        matrix = np.loadtxt(payload.decode("ascii").splitlines(), dtype=np.float64)
        matrix = np.atleast_2d(matrix)
        if matrix.shape[1] != len(fields):
            raise ValueError("PCD ASCII column count does not match FIELDS.")
        values = {field: matrix[:, index] for index, field in enumerate(fields)}
        points = matrix.shape[0]
    elif mode == "binary_compressed":
        values = _decode_binary_compressed_pcd(
            payload,
            fields,
            sizes,
            types,
            counts,
            points,
        )
    else:
        raise ValueError(f"Unsupported PCD DATA mode: {mode}")

    if not {"x", "y", "z"}.issubset(values):
        raise ValueError("PCD must contain x, y and z fields.")
    coord = np.column_stack((values["x"], values["y"], values["z"])).astype(np.float32)

    if {"r", "g", "b"}.issubset(values):
        color = np.column_stack((values["r"], values["g"], values["b"])).astype(np.float32)
    elif "rgb" in values or "rgba" in values:
        field = "rgb" if "rgb" in values else "rgba"
        color = _decode_packed_rgb(values[field], types[fields.index(field)])
    else:
        raise ValueError(
            "PCD has no RGB fields. ScanNet200 inference requires real color data."
        )

    normal = None
    if {"normal_x", "normal_y", "normal_z"}.issubset(values):
        normal = np.column_stack(
            (values["normal_x"], values["normal_y"], values["normal_z"])
        ).astype(np.float32)
    elif {"nx", "ny", "nz"}.issubset(values):
        normal = np.column_stack((values["nx"], values["ny"], values["nz"])).astype(np.float32)
    if points and coord.shape[0] != points:
        raise ValueError("PCD point count does not match the header.")
    return Cloud(
        coord,
        color,
        normal,
        "input" if normal is not None else None,
        np.arange(coord.shape[0], dtype=np.int64),
        int(coord.shape[0]),
    )


def load_cloud(path: Path, invalid_policy: str = "drop") -> Cloud:
    suffix = path.suffix.lower()
    if suffix == ".ply":
        cloud = load_ply(path)
    elif suffix == ".pcd":
        cloud = load_pcd(path)
    else:
        raise ValueError("Only .ply and .pcd inputs are supported by this inference tool.")

    mask = np.isfinite(cloud.coord).all(1) & np.isfinite(cloud.color).all(1)
    if cloud.normal is not None:
        mask &= np.isfinite(cloud.normal).all(1)
    if not mask.all():
        invalid = int((~mask).sum())
        if invalid_policy == "error":
            raise ValueError(
                f"The input contains {invalid} non-finite points. Use "
                "--invalid-policy drop to remove them with source-index mapping."
            )
        cloud.coord = cloud.coord[mask]
        cloud.color = cloud.color[mask]
        if cloud.normal is not None:
            cloud.normal = cloud.normal[mask]
        cloud.source_index = cloud.source_index[mask]
        cloud.invalid_points = invalid
    if cloud.coord.shape[0] == 0:
        raise ValueError("The input contains no finite points.")
    if cloud.color.max() <= 1.0:
        cloud.color = cloud.color * 255.0
    cloud.color = np.clip(cloud.color, 0.0, 255.0).astype(np.float32)
    return cloud


UP_AXIS_ROTATIONS = {
    "z": np.eye(3, dtype=np.float32),
    "-z": np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]], dtype=np.float32),
    "y": np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float32),
    "-y": np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], dtype=np.float32),
    "x": np.array([[0, -1, 0], [0, 0, 1], [1, 0, 0]], dtype=np.float32),
    "-x": np.array([[0, 1, 0], [0, 0, 1], [-1, 0, 0]], dtype=np.float32),
}


def apply_up_axis(cloud: Cloud, up_axis: str) -> Cloud:
    rotation = UP_AXIS_ROTATIONS[up_axis]
    cloud.coord = (cloud.coord @ rotation.T).astype(np.float32)
    if cloud.normal is not None:
        cloud.normal = (cloud.normal @ rotation.T).astype(np.float32)
    return cloud


def detect_up_axis(cloud: Cloud) -> tuple[str, dict[str, float]]:
    """Choose the direction that most resembles a room's lower horizontal support."""
    if cloud.normal is None:
        raise ValueError("Up-axis detection requires normals.")
    diagnostics: dict[str, float] = {}
    for candidate, rotation in UP_AXIS_ROTATIONS.items():
        coord = cloud.coord @ rotation.T
        normal = cloud.normal @ rotation.T
        height = coord[:, 2]
        low, high = np.quantile(height, (0.02, 0.98))
        span = max(float(high - low), 1e-6)
        lower = height <= low + 0.15 * span
        horizontal = np.abs(normal[:, 2]) >= 0.85
        lower_support = float(np.mean(lower & horizontal))
        horizontal_elsewhere = max(float(np.mean(horizontal)) - lower_support, 0.0)
        upward_support = float(np.mean(lower & (normal[:, 2] >= 0.75)))
        diagnostics[candidate] = (
            lower_support - 0.5 * horizontal_elsewhere + 0.1 * upward_support
        )
    selected = max(diagnostics, key=diagnostics.get)
    return selected, diagnostics


def estimate_normals(coord: np.ndarray, neighbors: int) -> np.ndarray:
    if coord.shape[0] < 3:
        raise ValueError("At least three points are required to estimate normals.")
    neighbors = min(max(3, neighbors), coord.shape[0])
    tree = cKDTree(coord)
    normals = np.empty_like(coord, dtype=np.float32)
    center = np.median(coord, axis=0)
    chunk_size = 50_000
    for start in range(0, coord.shape[0], chunk_size):
        stop = min(start + chunk_size, coord.shape[0])
        _, indices = tree.query(coord[start:stop], k=neighbors, workers=-1)
        local = coord[indices]
        local = local - local.mean(axis=1, keepdims=True)
        covariance = np.einsum("nki,nkj->nij", local, local) / float(neighbors)
        _, vectors = np.linalg.eigh(covariance)
        normal = vectors[:, :, 0]
        direction = coord[start:stop] - center
        flip = np.einsum("ni,ni->n", normal, direction) < 0
        normal[flip] *= -1
        normals[start:stop] = normal
    length = np.linalg.norm(normals, axis=1, keepdims=True)
    return normals / np.maximum(length, 1e-8)


def prepare_input(cloud: Cloud, grid_size: float) -> dict:
    np.random.seed(0)
    transform = Compose(
        [
            dict(type="CenterShift", apply_z=True),
            dict(
                type="GridSample",
                grid_size=grid_size,
                hash_type="fnv",
                mode="train",
                return_grid_coord=True,
                return_inverse=True,
            ),
            dict(type="CenterShift", apply_z=False),
            dict(type="NormalizeColor"),
            dict(type="ToTensor"),
            dict(type="Update", keys_dict={"grid_size": grid_size}),
            dict(
                type="Collect",
                keys=("coord", "grid_coord", "inverse", "grid_size"),
                feat_keys=("color", "normal"),
            ),
        ]
    )
    return transform(
        {
            "coord": cloud.coord.copy(),
            "color": cloud.color.copy(),
            "normal": cloud.normal.copy(),
        }
    )


def load_model(config_path: Path, weight_path: Path) -> tuple[torch.nn.Module, Config]:
    config = Config.fromfile(config_path)
    model = build_model(config.model)
    checkpoint = torch.load(weight_path, map_location="cpu", weights_only=False)
    state = checkpoint.get("state_dict", checkpoint)
    state = OrderedDict(
        (key.removeprefix("module."), value) for key, value in state.items()
    )
    model.load_state_dict(state, strict=True)
    return model.cuda().eval(), config


def build_point_labels(
    output: dict,
    inverse: np.ndarray,
    min_confidence: float,
) -> tuple[np.ndarray, list[dict], np.ndarray]:
    probabilities = torch.softmax(output["seg_logits"], dim=1)
    semantic_conf, semantic = probabilities.max(dim=1)
    semantic = semantic.detach().cpu().numpy().astype(np.int32)
    semantic_conf = semantic_conf.detach().cpu().numpy().astype(np.float32)

    sampled_instance = np.full(semantic.shape[0], -1, dtype=np.int32)
    sampled_confidence = semantic_conf.copy()
    accepted: list[dict] = []
    scores = output["pred_scores"].detach().cpu().numpy()
    classes = output["pred_classes"].detach().cpu().numpy().astype(np.int32)
    masks = output["pred_masks"].detach().cpu().numpy().astype(bool)
    for proposal in np.argsort(-scores):
        score = float(scores[proposal])
        if score < min_confidence:
            continue
        mask = masks[proposal] & (sampled_instance < 0)
        if not mask.any():
            continue
        instance_id = len(accepted)
        sampled_instance[mask] = instance_id
        sampled_confidence[mask] = score
        accepted.append(
            {
                "instance_id": instance_id,
                "semantic_id": int(classes[proposal]),
                "score": score,
                "sampled_points": int(mask.sum()),
            }
        )

    labels = np.empty(inverse.shape[0], dtype=LABEL_DTYPE)
    labels["semantic_id"] = semantic[inverse]
    labels["instance_id"] = sampled_instance[inverse]
    labels["confidence"] = sampled_confidence[inverse]
    return labels, accepted, semantic_conf[inverse]


def infer_cloud_once(
    model: torch.nn.Module,
    cloud: Cloud,
    grid_size: float,
    min_confidence: float,
) -> tuple[np.ndarray, list[dict], np.ndarray, int, float, int]:
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
        output = model(model_input)
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - started
    peak_memory = int(torch.cuda.max_memory_allocated())
    labels, instances, semantic_confidence = build_point_labels(
        output,
        inverse,
        min_confidence,
    )
    del output
    del model_input
    return labels, instances, semantic_confidence, sampled_points, elapsed, peak_memory


def filter_small_instances(
    labels: np.ndarray,
    instances: list[dict],
    semantic_confidence: np.ndarray,
    min_instance_points: int,
) -> tuple[np.ndarray, list[dict]]:
    retained: list[dict] = []
    for instance in instances:
        old_id = int(instance["instance_id"])
        mask = labels["instance_id"] == old_id
        points = int(mask.sum())
        if points < min_instance_points:
            labels["instance_id"][mask] = -1
            labels["confidence"][mask] = semantic_confidence[mask]
            continue
        new_id = len(retained)
        labels["instance_id"][mask] = new_id
        value = dict(instance)
        value["instance_id"] = new_id
        value["points"] = points
        retained.append(value)
    return labels, retained


def _voxel_components(coord: np.ndarray, voxel_size: float) -> list[np.ndarray]:
    """Return point-index components using 26-connected occupied voxels."""
    if coord.shape[0] == 0:
        return []
    voxel = np.floor(coord / voxel_size).astype(np.int64)
    unique_voxel, inverse = np.unique(voxel, axis=0, return_inverse=True)
    lookup = {tuple(value): index for index, value in enumerate(unique_voxel)}
    component_by_voxel = np.full(unique_voxel.shape[0], -1, dtype=np.int32)
    offsets = [
        (dx, dy, dz)
        for dx in (-1, 0, 1)
        for dy in (-1, 0, 1)
        for dz in (-1, 0, 1)
        if (dx, dy, dz) != (0, 0, 0)
    ]
    component_id = 0
    for seed in range(unique_voxel.shape[0]):
        if component_by_voxel[seed] >= 0:
            continue
        component_by_voxel[seed] = component_id
        stack = [seed]
        while stack:
            current = stack.pop()
            x, y, z = unique_voxel[current]
            for dx, dy, dz in offsets:
                neighbor = lookup.get((x + dx, y + dy, z + dz))
                if neighbor is not None and component_by_voxel[neighbor] < 0:
                    component_by_voxel[neighbor] = component_id
                    stack.append(neighbor)
        component_id += 1
    point_component = component_by_voxel[inverse]
    return [np.flatnonzero(point_component == value) for value in range(component_id)]


def _bbox_gap(left_min, left_max, right_min, right_max) -> float:
    gap = np.maximum(np.maximum(left_min - right_max, right_min - left_max), 0.0)
    return float(np.linalg.norm(gap))


def postprocess_instances(
    coord: np.ndarray,
    labels: np.ndarray,
    instances: list[dict],
    voxel_size: float = 0.08,
    component_min_points: int = 20,
    component_min_ratio: float = 0.01,
    merge_distance: float = 0.04,
) -> tuple[np.ndarray, list[dict], dict]:
    """Clean disconnected outliers, split components, and merge touching peers."""
    original_instance_ids = labels["instance_id"].copy()
    labels = labels.copy()
    labels["instance_id"] = -1
    candidates: list[dict] = []
    removed_points = 0
    split_instances = 0
    for instance in instances:
        original_id = int(instance["instance_id"])
        point_indices = np.flatnonzero(original_instance_ids == original_id)
        if point_indices.size == 0:
            continue
        local_components = _voxel_components(coord[point_indices], voxel_size)
        local_components.sort(key=len, reverse=True)
        kept = []
        for rank, local in enumerate(local_components):
            threshold = max(component_min_points, int(np.ceil(point_indices.size * component_min_ratio)))
            if rank == 0 or local.size >= threshold:
                kept.append(point_indices[local])
            else:
                removed_points += int(local.size)
        if len(kept) > 1:
            split_instances += len(kept) - 1
        for indices in kept:
            points = coord[indices]
            candidates.append(
                {
                    "semantic_id": int(instance["semantic_id"]),
                    "score": float(instance.get("score", labels["confidence"][indices].mean())),
                    "indices": indices,
                    "bbox_min": points.min(axis=0),
                    "bbox_max": points.max(axis=0),
                }
            )

    merged_pairs = 0
    disjoint = _DisjointSet(len(candidates))
    if merge_distance > 0:
        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                a, b = candidates[left], candidates[right]
                if a["semantic_id"] != b["semantic_id"]:
                    continue
                if _bbox_gap(a["bbox_min"], a["bbox_max"], b["bbox_min"], b["bbox_max"]) > merge_distance:
                    continue
                left_points = coord[a["indices"]]
                right_points = coord[b["indices"]]
                if left_points.shape[0] > right_points.shape[0]:
                    left_points, right_points = right_points, left_points
                if left_points.shape[0] > 5000:
                    selection = np.linspace(0, left_points.shape[0] - 1, 5000).astype(np.int64)
                    left_points = left_points[selection]
                distance, _ = cKDTree(right_points).query(left_points, k=1, workers=-1)
                if float(distance.min()) <= merge_distance:
                    disjoint.union(left, right)
                    merged_pairs += 1

    groups: dict[int, list[int]] = {}
    for index in range(len(candidates)):
        groups.setdefault(disjoint.find(index), []).append(index)
    output_instances = []
    ordered_groups = sorted(
        groups.values(),
        key=lambda members: int(min(candidates[value]["indices"].min() for value in members)),
    )
    for new_id, members in enumerate(ordered_groups):
        indices = np.unique(np.concatenate([candidates[value]["indices"] for value in members]))
        semantic_id = int(candidates[members[0]]["semantic_id"])
        labels["instance_id"][indices] = new_id
        output_instances.append(
            {
                "instance_id": new_id,
                "semantic_id": semantic_id,
                "score": float(max(candidates[value]["score"] for value in members)),
                "points": int(indices.size),
            }
        )
    return labels, output_instances, {
        "enabled": True,
        "voxel_size_m": voxel_size,
        "component_min_points": component_min_points,
        "component_min_ratio": component_min_ratio,
        "merge_distance_m": merge_distance,
        "removed_outlier_points": removed_points,
        "created_split_instances": split_instances,
        "merged_pairs": merged_pairs,
    }


def emit_progress(stage: str, progress: float, **details: object) -> None:
    event = {
        "schema_version": SCHEMA_VERSION,
        "type": "progress",
        "stage": stage,
        "progress": round(float(np.clip(progress, 0.0, 1.0)), 4),
        **details,
    }
    print("CLOUDFORGE_EVENT " + json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)


def is_cuda_out_of_memory(error: BaseException) -> bool:
    return isinstance(error, torch.cuda.OutOfMemoryError) or (
        isinstance(error, RuntimeError)
        and "out of memory" in str(error).lower()
        and "cuda" in str(error).lower()
    )


def infer_cloud_tiled(
    model: torch.nn.Module,
    cloud: Cloud,
    args: argparse.Namespace,
) -> tuple[np.ndarray, list[dict], int, float, int, list[SpatialTile], float, int, list[dict]]:
    requested_size = float(args.tile_size)
    oom_retries = 0
    adaptive_attempts: list[dict] = []
    while True:
        tiles, actual_size, attempts = build_adaptive_xy_tiles(
            cloud.coord,
            requested_size,
            args.tile_overlap,
            args.max_tile_points,
            args.min_tile_size,
        )
        adaptive_attempts.extend(
            {**attempt, "oom_retry": oom_retries} for attempt in attempts
        )
        predictions: list[TilePrediction] = []
        sampled_points = 0
        elapsed = 0.0
        peak_memory = 0
        try:
            for tile_number, tile in enumerate(tiles, start=1):
                tile_cloud = Cloud(
                    coord=cloud.coord[tile.indices],
                    color=cloud.color[tile.indices],
                    normal=cloud.normal[tile.indices],
                )
                (
                    tile_labels,
                    tile_instances,
                    tile_semantic_confidence,
                    tile_sampled_points,
                    tile_elapsed,
                    tile_peak_memory,
                ) = infer_cloud_once(
                    model,
                    tile_cloud,
                    args.grid_size,
                    args.min_confidence,
                )
                predictions.append(
                    TilePrediction(
                        tile,
                        tile_labels,
                        tile_semantic_confidence,
                        tile_instances,
                    )
                )
                sampled_points += tile_sampled_points
                elapsed += tile_elapsed
                peak_memory = max(peak_memory, tile_peak_memory)
                emit_progress(
                    "inference",
                    0.16 + 0.68 * tile_number / max(len(tiles), 1),
                    tile=tile_number,
                    tiles=len(tiles),
                    tile_points=int(tile.indices.shape[0]),
                    sampled_points=tile_sampled_points,
                    tile_size_m=actual_size,
                    oom_retry=oom_retries,
                )
                torch.cuda.empty_cache()
        except Exception as exc:
            if not is_cuda_out_of_memory(exc):
                raise
            next_size = actual_size * 0.5
            if (
                oom_retries >= args.oom_retries
                or next_size < args.min_tile_size
                or next_size <= args.tile_overlap
            ):
                raise RuntimeError(
                    "CUDA ran out of memory during tiled inference and automatic "
                    f"recovery stopped at {actual_size:g} m tiles after {oom_retries} "
                    "retries. Reduce --min-tile-size or --tile-overlap, lower "
                    "--max-tile-points, or increase --grid-size."
                ) from exc
            oom_retries += 1
            emit_progress(
                "oom_retry",
                0.16,
                retry=oom_retries,
                previous_tile_size_m=actual_size,
                next_tile_size_m=next_size,
            )
            predictions.clear()
            gc.collect()
            torch.cuda.empty_cache()
            requested_size = next_size
            continue

        emit_progress("tile_fusion", 0.87, tiles=len(tiles))
        labels, instances = fuse_tiled_predictions(
            int(cloud.coord.shape[0]),
            predictions,
            min_instance_points=args.min_instance_points,
            merge_ratio=args.tile_merge_ratio,
            merge_iou=args.tile_merge_iou,
            min_shared_points=args.tile_min_shared_points,
        )
        return (
            labels,
            instances,
            sampled_points,
            elapsed,
            peak_memory,
            tiles,
            actual_size,
            oom_retries,
            adaptive_attempts,
        )


def instance_colors(instance_id: np.ndarray, semantic_id: np.ndarray) -> np.ndarray:
    colors = np.full((instance_id.shape[0], 3), 110, dtype=np.uint8)
    assigned = instance_id >= 0
    value = instance_id[assigned].astype(np.uint32) + 1
    colors[assigned, 0] = ((value * 67 + 41) % 205 + 50).astype(np.uint8)
    colors[assigned, 1] = ((value * 97 + 83) % 205 + 50).astype(np.uint8)
    colors[assigned, 2] = ((value * 131 + 17) % 205 + 50).astype(np.uint8)
    stuff = ~assigned
    colors[stuff, 0] = ((semantic_id[stuff] * 29 + 60) % 150 + 50).astype(np.uint8)
    return colors


def write_labeled_ply(
    path: Path,
    coord: np.ndarray,
    labels: np.ndarray,
    source_index: np.ndarray | None = None,
) -> None:
    dtype = [
        ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
        ("source_index", "<u4"),
        ("semantic_id", "<i4"), ("instance_id", "<i4"), ("confidence", "<f4"),
    ]
    vertex = np.empty(coord.shape[0], dtype=dtype)
    vertex["x"], vertex["y"], vertex["z"] = coord.T
    color = instance_colors(labels["instance_id"], labels["semantic_id"])
    vertex["red"], vertex["green"], vertex["blue"] = color.T
    vertex["source_index"] = (
        np.arange(coord.shape[0], dtype=np.int64)
        if source_index is None
        else source_index
    )
    vertex["semantic_id"] = labels["semantic_id"]
    vertex["instance_id"] = labels["instance_id"]
    vertex["confidence"] = labels["confidence"]
    temporary = _temporary_output_path(path)
    PlyData([PlyElement.describe(vertex, "vertex")], text=False).write(str(temporary))
    temporary.replace(path)


def write_instance_ply(
    path: Path,
    coord: np.ndarray,
    color: np.ndarray,
    labels: np.ndarray,
    source_index: np.ndarray,
) -> None:
    dtype = [
        ("x", "<f4"), ("y", "<f4"), ("z", "<f4"),
        ("red", "u1"), ("green", "u1"), ("blue", "u1"),
        ("source_index", "<u4"),
        ("semantic_id", "<i4"), ("instance_id", "<i4"), ("confidence", "<f4"),
    ]
    vertex = np.empty(coord.shape[0], dtype=dtype)
    vertex["x"], vertex["y"], vertex["z"] = coord.T
    rgb = np.clip(np.rint(color), 0, 255).astype(np.uint8)
    vertex["red"], vertex["green"], vertex["blue"] = rgb.T
    vertex["source_index"] = source_index
    vertex["semantic_id"] = labels["semantic_id"]
    vertex["instance_id"] = labels["instance_id"]
    vertex["confidence"] = labels["confidence"]
    temporary = _temporary_output_path(path)
    PlyData([PlyElement.describe(vertex, "vertex")], text=False).write(str(temporary))
    temporary.replace(path)


def safe_class_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_")
    return cleaned or "object"


def _temporary_output_path(path: Path) -> Path:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.unlink(missing_ok=True)
    return temporary


def write_json(path: Path, value: object) -> None:
    temporary = _temporary_output_path(path)
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def write_binary_array(path: Path, value: np.ndarray) -> None:
    temporary = _temporary_output_path(path)
    value.tofile(temporary)
    temporary.replace(path)


def prepare_output_dir(path: Path, overwrite: bool) -> None:
    known_files = {
        "request.json", "run.json", "instances.json", "point_labels.bin",
        "source_index.bin", "labeled_cloud.ply",
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
    if args.grid_size <= 0:
        raise ValueError("--grid-size must be positive.")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    if not 0.0 <= args.min_confidence <= 1.0:
        raise ValueError("--min-confidence must be between 0 and 1.")
    if args.tile_threshold < 1:
        raise ValueError("--tile-threshold must be positive.")
    if args.tile_size <= 0:
        raise ValueError("--tile-size must be positive.")
    if args.tile_overlap < 0 or args.tile_overlap >= args.tile_size:
        raise ValueError("--tile-overlap must be non-negative and smaller than --tile-size.")
    if args.max_tile_points < 1:
        raise ValueError("--max-tile-points must be positive.")
    if args.min_tile_size <= args.tile_overlap or args.min_tile_size > args.tile_size:
        raise ValueError(
            "--min-tile-size must be greater than --tile-overlap and not exceed --tile-size."
        )
    if args.oom_retries < 0:
        raise ValueError("--oom-retries must be non-negative.")
    if not 0.0 <= args.tile_merge_ratio <= 1.0:
        raise ValueError("--tile-merge-ratio must be between 0 and 1.")
    if not 0.0 <= args.tile_merge_iou <= 1.0:
        raise ValueError("--tile-merge-iou must be between 0 and 1.")
    if args.tile_min_shared_points < 1 or args.min_instance_points < 1:
        raise ValueError("tile and instance point thresholds must be positive.")
    if args.component_voxel_size <= 0:
        raise ValueError("--component-voxel-size must be positive.")
    if args.component_min_points < 1:
        raise ValueError("--component-min-points must be positive.")
    if not 0.0 <= args.component_min_ratio <= 1.0:
        raise ValueError("--component-min-ratio must be between 0 and 1.")
    if args.instance_merge_distance < 0:
        raise ValueError("--instance-merge-distance must be non-negative.")

    # Fully parse the source before an explicit overwrite can remove old artifacts.
    cloud = load_cloud(args.input, args.invalid_policy)
    prepare_output_dir(args.output_dir, args.overwrite_output)
    args._output_prepared = True
    request = {
        "schema_version": SCHEMA_VERSION,
        "task": "instance_segmentation",
        "input": str(args.input.resolve()),
        "weight": str(args.weight.resolve()),
        "config": str(args.config.resolve()),
        "input_unit": args.input_unit,
        "grid_size_m": args.grid_size,
        "seed": args.seed,
        "normal_k": args.normal_k,
        "min_confidence": args.min_confidence,
        "min_instance_points": args.min_instance_points,
        "up_axis": args.up_axis,
        "invalid_policy": args.invalid_policy,
        "export_instances": args.export_instances,
        "postprocessing": {
            "enabled": args.postprocess,
            "component_voxel_size_m": args.component_voxel_size,
            "component_min_points": args.component_min_points,
            "component_min_ratio": args.component_min_ratio,
            "instance_merge_distance_m": args.instance_merge_distance,
        },
        "tiling": {
            "requested": args.tiling,
            "tile_threshold_points": args.tile_threshold,
            "tile_size_m": args.tile_size,
            "tile_overlap_m": args.tile_overlap,
            "max_tile_points": args.max_tile_points,
            "min_tile_size_m": args.min_tile_size,
            "oom_retries": args.oom_retries,
            "merge_ratio": args.tile_merge_ratio,
            "merge_iou": args.tile_merge_iou,
            "min_shared_points": args.tile_min_shared_points,
        },
    }
    write_json(args.output_dir / "request.json", request)
    emit_progress("validation", 0.02)
    unit_scale = {"m": 1.0, "cm": 0.01, "mm": 0.001}[args.input_unit]
    cloud.coord = cloud.coord.astype(np.float32) * unit_scale
    normals_source = cloud.normal_source or "estimated_knn"
    emit_progress(
        "normals",
        0.06,
        points=int(cloud.coord.shape[0]),
        source=normals_source,
    )
    if cloud.normal is None:
        cloud.normal = estimate_normals(cloud.coord, args.normal_k)
    else:
        length = np.linalg.norm(cloud.normal, axis=1, keepdims=True)
        cloud.normal = cloud.normal / np.maximum(length, 1e-8)

    if args.up_axis == "auto":
        selected_up_axis, up_axis_scores = detect_up_axis(cloud)
    else:
        selected_up_axis = args.up_axis
        up_axis_scores = {}
    apply_up_axis(cloud, selected_up_axis)
    emit_progress("orientation", 0.12, selected_up_axis=selected_up_axis)

    emit_progress("model", 0.16)
    model, config = load_model(args.config, args.weight)
    tiling_enabled = args.tiling == "on" or (
        args.tiling == "auto" and cloud.coord.shape[0] > args.tile_threshold
    )
    tile_count = 1
    actual_tile_size = float(args.tile_size)
    oom_retries = 0
    adaptive_tile_attempts: list[dict] = []
    if tiling_enabled:
        (
            labels,
            instances,
            sampled_points,
            elapsed,
            peak_memory,
            tiles,
            actual_tile_size,
            oom_retries,
            adaptive_tile_attempts,
        ) = infer_cloud_tiled(model, cloud, args)
        tile_count = len(tiles)
    else:
        emit_progress("inference", 0.22, tiles=1)
        try:
            (
                labels,
                instances,
                semantic_confidence,
                sampled_points,
                elapsed,
                peak_memory,
            ) = infer_cloud_once(
                model,
                cloud,
                args.grid_size,
                args.min_confidence,
            )
        except Exception as exc:
            if args.tiling == "off" or not is_cuda_out_of_memory(exc):
                if is_cuda_out_of_memory(exc):
                    raise RuntimeError(
                        "CUDA ran out of memory during whole-scene inference. "
                        "Rerun with --tiling on or --tiling auto."
                    ) from exc
                raise
            emit_progress(
                "oom_switch_to_tiling",
                0.16,
                tile_size_m=args.tile_size,
            )
            gc.collect()
            torch.cuda.empty_cache()
            tiling_enabled = True
            (
                labels,
                instances,
                sampled_points,
                elapsed,
                peak_memory,
                tiles,
                actual_tile_size,
                oom_retries,
                adaptive_tile_attempts,
            ) = infer_cloud_tiled(model, cloud, args)
            tile_count = len(tiles)
        else:
            labels, instances = filter_small_instances(
                labels,
                instances,
                semantic_confidence,
                args.min_instance_points,
            )
            emit_progress("inference", 0.84, tiles=1, sampled_points=sampled_points)

    if args.postprocess:
        emit_progress("postprocessing", 0.88, instances=len(instances))
        labels, instances, postprocessing = postprocess_instances(
            cloud.coord,
            labels,
            instances,
            voxel_size=args.component_voxel_size,
            component_min_points=args.component_min_points,
            component_min_ratio=args.component_min_ratio,
            merge_distance=args.instance_merge_distance,
        )
    else:
        postprocessing = {"enabled": False}
    emit_progress("export", 0.91, instances=len(instances))
    names = list(config.data.names)
    for instance in instances:
        mask = labels["instance_id"] == instance["instance_id"]
        points = cloud.coord[mask]
        instance["class_name"] = names[instance["semantic_id"]]
        instance["points"] = int(mask.sum())
        instance["bbox_min_m"] = points.min(axis=0).astype(float).tolist()
        instance["bbox_max_m"] = points.max(axis=0).astype(float).tolist()
    semantic_ids, semantic_counts = np.unique(labels["semantic_id"], return_counts=True)
    semantic_histogram = [
        {
            "semantic_id": int(semantic_id),
            "class_name": (
                names[int(semantic_id)]
                if 0 <= int(semantic_id) < len(names)
                else "unknown"
            ),
            "points": int(count),
        }
        for semantic_id, count in sorted(
            zip(semantic_ids, semantic_counts), key=lambda item: -item[1]
        )
    ]

    full_labels = np.empty(cloud.original_points, dtype=LABEL_DTYPE)
    full_labels["semantic_id"] = -1
    full_labels["instance_id"] = -1
    full_labels["confidence"] = 0.0
    full_labels[cloud.source_index] = labels
    write_binary_array(args.output_dir / "point_labels.bin", full_labels)
    write_binary_array(
        args.output_dir / "source_index.bin",
        cloud.source_index.astype("<i8"),
    )
    write_labeled_ply(
        args.output_dir / "labeled_cloud.ply",
        cloud.coord,
        labels,
        cloud.source_index,
    )
    if args.export_instances:
        instance_dir = args.output_dir / "instances"
        instance_dir.mkdir(parents=True, exist_ok=True)
        for instance in instances:
            mask = labels["instance_id"] == instance["instance_id"]
            filename = (
                f"instance_{instance['instance_id']:03d}_"
                f"{safe_class_name(instance['class_name'])}.ply"
            )
            write_instance_ply(
                instance_dir / filename,
                cloud.coord[mask],
                cloud.color[mask],
                labels[mask],
                cloud.source_index[mask],
            )
            instance["ply"] = str((instance_dir / filename).resolve())
    write_json(
        args.output_dir / "instances.json",
        {
            "schema_version": SCHEMA_VERSION,
            "task": "instance_segmentation",
            "instances": instances,
        },
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "task": "instance_segmentation",
        "input": str(args.input.resolve()),
        "weight": str(args.weight.resolve()),
        "config": str(args.config.resolve()),
        "input_unit": args.input_unit,
        "output_unit": "m",
        "requested_up_axis": args.up_axis,
        "selected_up_axis": selected_up_axis,
        "up_axis_scores": up_axis_scores,
        "normal_source": normals_source,
        "input_points": int(cloud.original_points),
        "valid_points": int(cloud.coord.shape[0]),
        "invalid_points": int(cloud.invalid_points),
        "sampled_points": sampled_points,
        "instances": len(instances),
        "tiling": {
            "requested": args.tiling,
            "enabled": tiling_enabled,
            "tile_count": tile_count,
            "tile_threshold_points": args.tile_threshold,
            "requested_tile_size_m": args.tile_size,
            "actual_tile_size_m": actual_tile_size,
            "tile_overlap_m": args.tile_overlap,
            "max_tile_points": args.max_tile_points,
            "min_tile_size_m": args.min_tile_size,
            "oom_retries": oom_retries,
            "adaptive_attempts": adaptive_tile_attempts,
            "merge_ratio": args.tile_merge_ratio,
            "merge_iou": args.tile_merge_iou,
            "min_shared_points": args.tile_min_shared_points,
        },
        "min_instance_points": args.min_instance_points,
        "seed": args.seed,
        "postprocessing": postprocessing,
        "semantic_histogram": semantic_histogram,
        "elapsed_seconds": elapsed,
        "total_elapsed_seconds": time.perf_counter() - total_started,
        "peak_gpu_memory_bytes": peak_memory,
        "label_record": "little-endian int32 semantic_id, int32 instance_id, float32 confidence; one record per original point",
        "source_index_record": "little-endian int64 original point index; one record per valid point",
    }
    result["warnings"] = []
    required_proposal_points = int(config.model.cluster_propose_points)
    if not tiling_enabled and sampled_points < required_proposal_points:
        result["warnings"].append(
            f"Only {sampled_points} points remain after {args.grid_size:g} m grid sampling; "
            f"the model requires at least {required_proposal_points} sampled points to propose an instance. "
            "Check units and model/domain suitability."
        )
    write_json(args.output_dir / "run.json", result)
    emit_progress("complete", 1.0, instances=len(instances))
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
