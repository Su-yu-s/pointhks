from pathlib import Path
from tempfile import TemporaryDirectory
import struct
import sys
import unittest

import numpy as np
from plyfile import PlyData, PlyElement


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.infer_scannet200_instances import (
    LABEL_DTYPE,
    Cloud,
    SpatialTile,
    TilePrediction,
    apply_up_axis,
    build_adaptive_xy_tiles,
    build_xy_tiles,
    detect_up_axis,
    fuse_tiled_predictions,
    is_cuda_out_of_memory,
    load_cloud,
    postprocess_instances,
    write_labeled_ply,
)
from tools.litept_profiles import DEFAULT_PROFILE_FILE, load_profiles, validate_profile_files


class InstanceIoTest(unittest.TestCase):
    @staticmethod
    def _lzf_literal(data):
        encoded = bytearray()
        for start in range(0, len(data), 32):
            chunk = data[start : start + 32]
            encoded.append(len(chunk) - 1)
            encoded.extend(chunk)
        return bytes(encoded)

    @staticmethod
    def _tile_prediction(tile, instance_ids, semantic_id=8, score=0.8):
        labels = np.empty(len(instance_ids), dtype=LABEL_DTYPE)
        labels["semantic_id"] = semantic_id
        labels["instance_id"] = instance_ids
        labels["confidence"] = score
        local_ids = sorted(set(instance_ids) - {-1})
        instances = [
            {
                "instance_id": instance_id,
                "semantic_id": semantic_id,
                "score": score,
            }
            for instance_id in local_ids
        ]
        return TilePrediction(
            tile,
            labels,
            np.full(len(instance_ids), 0.9, dtype=np.float32),
            instances,
        )

    def test_rgb_ply_round_trip(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.ply"
            vertex = np.array(
                [(0.0, 0.0, 0.0, 10, 20, 30), (1.0, 2.0, 3.0, 40, 50, 60)],
                dtype=[("x", "f4"), ("y", "f4"), ("z", "f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")],
            )
            PlyData([PlyElement.describe(vertex, "vertex")], text=False).write(source)
            cloud = load_cloud(source)
            self.assertEqual(cloud.coord.shape, (2, 3))
            np.testing.assert_array_equal(cloud.color[1], [40, 50, 60])

            labels = np.zeros(2, dtype=LABEL_DTYPE)
            labels["semantic_id"] = [1, 2]
            labels["instance_id"] = [0, -1]
            labels["confidence"] = [0.8, 0.6]
            output = Path(directory) / "output.ply"
            write_labeled_ply(output, cloud.coord, labels)
            result = PlyData.read(output)["vertex"].data
            np.testing.assert_array_equal(result["instance_id"], [0, -1])
            np.testing.assert_array_equal(result["source_index"], [0, 1])

    def test_non_finite_points_are_dropped_with_source_mapping(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.ply"
            vertex = np.array(
                [
                    (0.0, 0.0, 0.0, 10, 20, 30),
                    (float("nan"), 1.0, 2.0, 40, 50, 60),
                    (3.0, 4.0, 5.0, 70, 80, 90),
                ],
                dtype=[("x", "f4"), ("y", "f4"), ("z", "f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")],
            )
            PlyData([PlyElement.describe(vertex, "vertex")], text=False).write(source)
            cloud = load_cloud(source)
            self.assertEqual(cloud.original_points, 3)
            self.assertEqual(cloud.invalid_points, 1)
            np.testing.assert_array_equal(cloud.source_index, [0, 2])

    def test_auto_up_axis_detects_open3d_y_down_room(self):
        xx, yy = np.meshgrid(np.linspace(0, 2, 20), np.linspace(0, 2, 20))
        floor_z_up = np.column_stack((xx.ravel(), yy.ravel(), np.zeros(xx.size)))
        floor_normal = np.tile([0.0, 0.0, 1.0], (floor_z_up.shape[0], 1))
        wall_z_up = np.column_stack((np.zeros(xx.size), xx.ravel(), yy.ravel()))
        wall_normal = np.tile([1.0, 0.0, 0.0], (wall_z_up.shape[0], 1))
        coord_z_up = np.vstack((floor_z_up, wall_z_up)).astype(np.float32)
        normal_z_up = np.vstack((floor_normal, wall_normal)).astype(np.float32)
        # Inverse of the Open3D camera convention conversion: (x, z, -y) -> Z-up.
        coord_y_down = coord_z_up[:, [0, 2, 1]] * np.array([1, -1, 1], dtype=np.float32)
        normal_y_down = normal_z_up[:, [0, 2, 1]] * np.array([1, -1, 1], dtype=np.float32)
        cloud = Cloud(coord_y_down, np.zeros_like(coord_y_down), normal_y_down)
        selected, _ = detect_up_axis(cloud)
        self.assertEqual(selected, "-y")
        apply_up_axis(cloud, selected)
        np.testing.assert_allclose(cloud.coord, coord_z_up, atol=1e-6)

    def test_xyz_only_pcd_is_rejected(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "xyz.pcd"
            source.write_text(
                "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\n"
                "TYPE F F F\nCOUNT 1 1 1\nWIDTH 1\nHEIGHT 1\nPOINTS 1\n"
                "DATA ascii\n0 0 0\n",
                encoding="ascii",
            )
            with self.assertRaisesRegex(ValueError, "requires real color"):
                load_cloud(source)

    def test_binary_compressed_pcd_is_decoded(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "compressed.pcd"
            uncompressed = b"".join(
                (
                    struct.pack("<2f", 1.0, 4.0),
                    struct.pack("<2f", 2.0, 5.0),
                    struct.pack("<2f", 3.0, 6.0),
                    bytes((10, 40)),
                    bytes((20, 50)),
                    bytes((30, 60)),
                )
            )
            compressed = self._lzf_literal(uncompressed)
            header = (
                "# .PCD v0.7\nVERSION 0.7\n"
                "FIELDS x y z r g b\nSIZE 4 4 4 1 1 1\n"
                "TYPE F F F U U U\nCOUNT 1 1 1 1 1 1\n"
                "WIDTH 2\nHEIGHT 1\nPOINTS 2\nDATA binary_compressed\n"
            ).encode("ascii")
            source.write_bytes(
                header
                + struct.pack("<II", len(compressed), len(uncompressed))
                + compressed
            )
            cloud = load_cloud(source)
            np.testing.assert_allclose(cloud.coord, [[1, 2, 3], [4, 5, 6]])
            np.testing.assert_array_equal(cloud.color, [[10, 20, 30], [40, 50, 60]])

    def test_xy_tiles_include_neighboring_halo_points(self):
        coord = np.array(
            [
                [0.1, 0.1, 0.0],
                [0.9, 0.1, 0.0],
                [1.1, 0.1, 0.0],
                [1.9, 0.1, 0.0],
            ],
            dtype=np.float32,
        )
        tiles = build_xy_tiles(coord, tile_size=1.0, overlap=0.2)
        self.assertEqual(len(tiles), 2)
        np.testing.assert_array_equal(tiles[0].indices, [0, 1, 2])
        np.testing.assert_array_equal(tiles[1].indices, [1, 2, 3])

    def test_dense_tiles_are_adaptively_subdivided(self):
        coord = np.column_stack(
            (
                np.linspace(0.0, 4.0, 201, endpoint=False),
                np.zeros(201),
                np.zeros(201),
            )
        ).astype(np.float32)
        tiles, actual_size, attempts = build_adaptive_xy_tiles(
            coord,
            tile_size=4.0,
            overlap=0.1,
            max_tile_points=80,
            min_tile_size=0.5,
        )
        self.assertLess(actual_size, 4.0)
        self.assertGreater(len(attempts), 1)
        self.assertLessEqual(max(len(tile.indices) for tile in tiles), 80)

    def test_adaptive_tiles_try_exact_minimum_size(self):
        coord = np.column_stack(
            (
                np.linspace(0.0, 1.5, 201, endpoint=False),
                np.zeros(201),
                np.zeros(201),
            )
        ).astype(np.float32)
        tiles, actual_size, attempts = build_adaptive_xy_tiles(
            coord,
            tile_size=1.5,
            overlap=0.2,
            max_tile_points=180,
            min_tile_size=1.0,
        )
        self.assertEqual(actual_size, 1.0)
        self.assertEqual([value["tile_size_m"] for value in attempts], [1.5, 1.0])
        self.assertLessEqual(max(len(tile.indices) for tile in tiles), 180)

    def test_cuda_oom_detection_is_specific(self):
        self.assertTrue(is_cuda_out_of_memory(RuntimeError("CUDA out of memory")))
        self.assertFalse(is_cuda_out_of_memory(RuntimeError("CPU out of memory")))
        self.assertFalse(is_cuda_out_of_memory(ValueError("CUDA out of memory")))

    def test_tiled_instances_merge_when_overlap_assignments_agree(self):
        left = SpatialTile(0, 0, 0, np.array([0, 1, 2, 3], dtype=np.int64))
        right = SpatialTile(1, 1, 0, np.array([2, 3, 4, 5], dtype=np.int64))
        predictions = [
            self._tile_prediction(left, [0, 0, 0, 0], score=0.8),
            self._tile_prediction(right, [0, 0, 0, 0], score=0.9),
        ]
        labels, instances = fuse_tiled_predictions(
            6,
            predictions,
            min_instance_points=1,
            min_shared_points=2,
        )
        self.assertEqual(len(instances), 1)
        np.testing.assert_array_equal(labels["instance_id"], [0, 0, 0, 0, 0, 0])
        self.assertAlmostEqual(instances[0]["score"], 0.9)

    def test_tiled_instances_stay_separate_without_shared_assignments(self):
        left = SpatialTile(0, 0, 0, np.array([0, 1, 2, 3], dtype=np.int64))
        right = SpatialTile(1, 1, 0, np.array([2, 3, 4, 5], dtype=np.int64))
        predictions = [
            self._tile_prediction(left, [0, 0, 0, -1], score=0.8),
            self._tile_prediction(right, [-1, 0, 0, 0], score=0.9),
        ]
        labels, instances = fuse_tiled_predictions(
            6,
            predictions,
            min_instance_points=1,
            min_shared_points=1,
        )
        self.assertEqual(len(instances), 2)
        self.assertNotEqual(labels["instance_id"][1], labels["instance_id"][4])

    def test_instance_postprocessing_splits_and_removes_small_component(self):
        coord = np.array(
            [
                [0.00, 0.00, 0.00], [0.03, 0.00, 0.00], [0.00, 0.03, 0.00],
                [1.00, 0.00, 0.00], [1.03, 0.00, 0.00], [1.00, 0.03, 0.00],
                [4.00, 0.00, 0.00],
            ],
            dtype=np.float32,
        )
        labels = np.empty(7, dtype=LABEL_DTYPE)
        labels["semantic_id"] = 8
        labels["instance_id"] = 0
        labels["confidence"] = 0.9
        result, instances, report = postprocess_instances(
            coord,
            labels,
            [{"instance_id": 0, "semantic_id": 8, "score": 0.9}],
            voxel_size=0.08,
            component_min_points=2,
            component_min_ratio=0.0,
            merge_distance=0.0,
        )
        self.assertEqual(len(instances), 2)
        self.assertEqual(result["instance_id"][6], -1)
        self.assertEqual(report["removed_outlier_points"], 1)
        self.assertEqual(report["created_split_instances"], 1)

    def test_instance_postprocessing_merges_touching_same_class(self):
        coord = np.array(
            [[0.00, 0, 0], [0.02, 0, 0], [0.05, 0, 0], [0.07, 0, 0]],
            dtype=np.float32,
        )
        labels = np.empty(4, dtype=LABEL_DTYPE)
        labels["semantic_id"] = 8
        labels["instance_id"] = [0, 0, 1, 1]
        labels["confidence"] = 0.9
        result, instances, report = postprocess_instances(
            coord,
            labels,
            [
                {"instance_id": 0, "semantic_id": 8, "score": 0.8},
                {"instance_id": 1, "semantic_id": 8, "score": 0.9},
            ],
            voxel_size=0.03,
            component_min_points=1,
            component_min_ratio=0.0,
            merge_distance=0.031,
        )
        self.assertEqual(len(instances), 1)
        np.testing.assert_array_equal(result["instance_id"], [0, 0, 0, 0])
        self.assertEqual(report["merged_pairs"], 1)

    def test_built_in_inference_profiles_are_ready(self):
        profiles = load_profiles(DEFAULT_PROFILE_FILE)
        self.assertEqual(
            set(profiles),
            {
                "scannet200-instances",
                "scannet-instances",
                "scannet-semantic",
                "structured3d-semantic",
                "nuscenes-semantic",
            },
        )
        self.assertEqual(profiles["scannet200-instances"].task, "instance_segmentation")
        self.assertEqual(profiles["scannet-instances"].task, "instance_segmentation")
        self.assertEqual(profiles["scannet-semantic"].class_count, 20)
        self.assertEqual(profiles["structured3d-semantic"].class_count, 25)
        self.assertEqual(profiles["nuscenes-semantic"].task, "semantic_segmentation")
        for profile in profiles.values():
            self.assertEqual(validate_profile_files(profile), [])


if __name__ == "__main__":
    unittest.main()
