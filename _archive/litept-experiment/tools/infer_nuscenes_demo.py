"""Run the released NuScenes LitePT model and export a colored point cloud.

This is an integration demo for the current local sample.  Its labels are
valid only for the NuScenes road-scene classes, not for indoor or building
point clouds.
"""

from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageDraw

from datasets.transform import Compose
from models import build_model
from utils.config import Config


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
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=root / "weights" / "outdoor_sample1.bin",
        help="NuScenes-format float32 N×5 point cloud.",
    )
    parser.add_argument(
        "--weight",
        type=Path,
        default=root / "weights" / "model_best.pth",
        help="NuScenes semantic-segmentation checkpoint.",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=root / "outputs" / "nuscenes_demo"
    )
    return parser.parse_args()


def write_ply(path: Path, coord: np.ndarray, color: np.ndarray) -> None:
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("ply\nformat ascii 1.0\n")
        stream.write(f"element vertex {coord.shape[0]}\n")
        stream.write("property float x\nproperty float y\nproperty float z\n")
        stream.write("property uchar red\nproperty uchar green\nproperty uchar blue\n")
        stream.write("end_header\n")
        for point, rgb in zip(coord, color):
            stream.write(
                f"{point[0]:.6f} {point[1]:.6f} {point[2]:.6f} "
                f"{int(rgb[0])} {int(rgb[1])} {int(rgb[2])}\n"
            )


def write_preview(
    path: Path, coord: np.ndarray, labels: np.ndarray, names: list[str]
) -> None:
    """Create a top-down visual preview without adding a plotting dependency."""
    width, height = 1500, 1000
    canvas = Image.new("RGB", (width, height), (29, 35, 46))
    draw = ImageDraw.Draw(canvas)
    xy = coord[:, :2]
    lower, upper = xy.min(axis=0), xy.max(axis=0)
    span = np.maximum(upper - lower, 1e-6)
    margin, map_width, map_height = 40, 1040, 900
    px = margin + (xy[:, 0] - lower[0]) / span[0] * map_width
    py = margin + (upper[1] - xy[:, 1]) / span[1] * map_height
    for x, y, label in zip(px.astype(int), py.astype(int), labels):
        draw.point((x, y), fill=tuple(int(v) for v in COLORS[label]))

    draw.text((1120, 40), "LitePT NuScenes semantic output", fill=(245, 245, 245))
    draw.text((1120, 65), "top-down preview", fill=(190, 200, 210))
    counts = Counter(int(value) for value in labels)
    y = 110
    for label, count in sorted(counts.items(), key=lambda item: -item[1]):
        color = tuple(int(v) for v in COLORS[label])
        draw.rectangle((1120, y, 1138, y + 18), fill=color)
        draw.text((1150, y), f"{names[label]}: {count}", fill=(235, 235, 235))
        y += 28
    canvas.save(path)


def main() -> None:
    args = parse_args()
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU is required for this LitePT demo.")
    if not args.input.is_file() or not args.weight.is_file():
        raise FileNotFoundError("Input point cloud or checkpoint was not found.")

    root = Path(__file__).resolve().parents[1]
    config = Config.fromfile(root / "configs/nuscenes/semseg-litept-small-v1m1.py")
    names = list(config.names)
    model = build_model(config.model).cuda().eval()
    checkpoint = torch.load(args.weight, map_location="cpu", weights_only=False)
    state = OrderedDict(
        (key.removeprefix("module."), value)
        for key, value in checkpoint["state_dict"].items()
    )
    model.load_state_dict(state, strict=True)

    raw = np.fromfile(args.input, dtype=np.float32)
    if raw.size % 5 != 0:
        raise ValueError("Expected a NuScenes-format N×5 float32 .bin file.")
    raw = raw.reshape(-1, 5)
    coord = raw[:, :3]
    np.random.seed(0)
    transform = Compose(
        [
            dict(
                type="GridSample",
                grid_size=0.05,
                hash_type="fnv",
                mode="train",
                return_grid_coord=True,
                return_inverse=True,
            ),
            dict(type="ToTensor"),
            dict(type="Update", keys_dict={"grid_size": 0.05}),
            dict(
                type="Collect",
                keys=("coord", "grid_coord", "inverse", "grid_size"),
                feat_keys=("coord", "strength"),
            ),
        ]
    )
    point = transform({"coord": coord, "strength": raw[:, 3:4] / 255.0})
    sampled_count = point["coord"].shape[0]
    for key, value in point.items():
        if isinstance(value, torch.Tensor):
            point[key] = value.cuda(non_blocking=True)
    with torch.no_grad():
        logits = model(point)["seg_logits"]
        probabilities = torch.softmax(logits, dim=1)
        prediction = probabilities.argmax(dim=1)[point["inverse"]].cpu().numpy()
        confidence = probabilities.max(dim=1).values[point["inverse"]].cpu().numpy()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    colors = COLORS[prediction]
    write_ply(args.output_dir / "labeled_cloud.ply", coord, colors)
    np.savetxt(
        args.output_dir / "point_labels.csv",
        np.column_stack((np.arange(coord.shape[0]), prediction, confidence)),
        delimiter=",",
        header="point_id,semantic_id,confidence",
        comments="",
        fmt=["%d", "%d", "%.6f"],
    )
    write_preview(args.output_dir / "preview_topdown.png", coord, prediction, names)

    print(f"Input points: {coord.shape[0]}; grid-sampled: {sampled_count}")
    for label, count in sorted(Counter(prediction).items(), key=lambda item: -item[1]):
        print(f"{names[int(label)]}: {count}")
    print(f"PLY: {args.output_dir / 'labeled_cloud.ply'}")
    print(f"CSV: {args.output_dir / 'point_labels.csv'}")
    print(f"Preview: {args.output_dir / 'preview_topdown.png'}")


if __name__ == "__main__":
    main()
