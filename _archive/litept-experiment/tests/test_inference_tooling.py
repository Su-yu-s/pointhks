import json
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from batch_infer import load_manifest
from evaluate_labels import LABEL_DTYPE, evaluate
from litept_profiles import load_profiles
import litept_infer


class InferenceToolingTest(unittest.TestCase):
    def test_indoor_profiles_forward_units_to_backend(self):
        for task in ("instance_segmentation", "semantic_segmentation"):
            with self.subTest(task=task):
                profile = SimpleNamespace(
                    name="indoor", task=task, input={"formats": ["ply", "pcd"]},
                    entrypoint=Path("infer.py"), config=Path("config.py"), weight=Path("weight.pth"),
                )
                argv = ["litept_infer.py", "sample.ply", "--input-unit", "mm", "--grid-size", "0.01"]
                with patch.object(sys, "argv", argv), \
                     patch.object(litept_infer, "get_profile", return_value=profile), \
                     patch.object(litept_infer, "validate_profile_files", return_value=[]), \
                     patch.object(litept_infer.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                    with self.assertRaises(SystemExit) as exit_result:
                        litept_infer.main()
                    self.assertEqual(exit_result.exception.code, 0)
                    command = run.call_args.args[0]
                    self.assertEqual(command[command.index("--input-unit") + 1], "mm")
                    self.assertEqual(command[command.index("--grid-size") + 1], "0.01")

    def test_custom_profile_paths_are_relative_to_profile_file(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("run.py", "model.py", "weight.pth"):
                (root / relative).write_bytes(b"x")
            profile_file = root / "profiles.json"
            profile_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profiles": {
                            "custom": {
                                "description": "custom test",
                                "task": "semantic_segmentation",
                                "entrypoint": "run.py",
                                "config": "model.py",
                                "weight": "weight.pth",
                                "class_count": 2,
                                "domain": "test",
                                "input": {
                                    "formats": ["bin"],
                                    "required_fields": ["x", "y", "z"],
                                    "optional_fields": [],
                                    "default_unit": "m",
                                    "model_up_axis": "z",
                                },
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            profile = load_profiles(profile_file)["custom"]
            self.assertEqual(profile.weight, (root / "weight.pth").resolve())

    def test_label_evaluator_reports_perfect_result(self):
        labels = np.empty(4, dtype=LABEL_DTYPE)
        labels["semantic_id"] = [1, 1, 2, 2]
        labels["instance_id"] = [0, 0, 1, 1]
        labels["confidence"] = 1
        result = evaluate(labels, labels.copy())
        self.assertEqual(result["semantic"]["overall_accuracy"], 1.0)
        self.assertEqual(result["semantic"]["mean_iou"], 1.0)
        self.assertEqual(result["instance"]["f1"], 1.0)

    def test_batch_manifest_rejects_duplicate_output_directories(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "batch.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "jobs": [
                            {"id": "a", "input": "a.bin", "profile": "p", "output_dir": "out"},
                            {"id": "b", "input": "b.bin", "profile": "p", "output_dir": "out"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unique"):
                load_manifest(manifest)


if __name__ == "__main__":
    unittest.main()
