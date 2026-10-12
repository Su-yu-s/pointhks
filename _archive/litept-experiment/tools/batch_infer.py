"""Run a validated JSON manifest of LitePT inference jobs sequentially."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from cloudforge_profiles import DEFAULT_PROFILE_FILE, get_profile, validate_profile_files
from verify_inference_output import verify_output


SCHEMA_VERSION = 1
RESERVED_OPTIONS = {"--profile", "--profiles-file", "--output-dir"}


def _resolve(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def load_manifest(path: Path) -> list[dict]:
    path = path.resolve()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid batch manifest JSON: {exc}") from exc
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"Batch manifest schema_version must be {SCHEMA_VERSION}.")
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("Batch manifest must contain a non-empty jobs list.")
    ids, outputs = set(), set()
    normalized = []
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"Job {index} must be an object.")
        missing = {"id", "input", "profile", "output_dir"} - job.keys()
        if missing:
            raise ValueError(f"Job {index} is missing: {', '.join(sorted(missing))}")
        job_id = str(job["id"])
        if not job_id or job_id in ids:
            raise ValueError(f"Job IDs must be non-empty and unique: {job_id!r}")
        options = job.get("options", [])
        if not isinstance(options, list) or not all(isinstance(value, str) for value in options):
            raise ValueError(f"Job '{job_id}' options must be a list of strings.")
        if RESERVED_OPTIONS.intersection(options):
            raise ValueError(f"Job '{job_id}' options cannot override profile or output routing.")
        output_dir = _resolve(str(job["output_dir"]), path.parent)
        if output_dir in outputs:
            raise ValueError(f"Batch output directories must be unique: {output_dir}")
        ids.add(job_id)
        outputs.add(output_dir)
        normalized.append(
            {
                "id": job_id,
                "input": _resolve(str(job["input"]), path.parent),
                "profile": str(job["profile"]),
                "output_dir": output_dir,
                "options": options,
            }
        )
    return normalized


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_batch(
    manifest_path: Path,
    profiles_file: Path = DEFAULT_PROFILE_FILE,
    continue_on_error: bool = False,
) -> dict:
    jobs = load_manifest(manifest_path)
    results = []
    started = time.perf_counter()
    infer_script = Path(__file__).resolve().parent / "cloudforge_infer.py"
    for index, job in enumerate(jobs):
        profile = get_profile(job["profile"], profiles_file)
        problems = validate_profile_files(profile)
        if problems:
            raise RuntimeError(f"Profile '{profile.name}' is not ready: {'; '.join(problems)}")
        command = [
            sys.executable, str(infer_script), str(job["input"]),
            "--profile", profile.name,
            "--profiles-file", str(profiles_file),
            "--output-dir", str(job["output_dir"]),
            *job["options"],
        ]
        job_started = time.perf_counter()
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        result = {
            "id": job["id"],
            "index": index,
            "return_code": completed.returncode,
            "elapsed_seconds": time.perf_counter() - job_started,
            "output_dir": str(job["output_dir"]),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
        if completed.returncode == 0:
            try:
                result["verification"] = verify_output(job["output_dir"])
                result["status"] = "success"
            except Exception as exc:
                result["status"] = "verification_error"
                result["verification_error"] = str(exc)
        else:
            result["status"] = "inference_error"
        results.append(result)
        if result["status"] != "success" and not continue_on_error:
            break
    successful = sum(value["status"] == "success" for value in results)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success" if successful == len(jobs) else "partial_failure",
        "manifest": str(manifest_path.resolve()),
        "requested_jobs": len(jobs),
        "completed_jobs": len(results),
        "successful_jobs": successful,
        "elapsed_seconds": time.perf_counter() - started,
        "jobs": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--profiles-file", type=Path, default=DEFAULT_PROFILE_FILE)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run_batch(args.manifest, args.profiles_file, args.continue_on_error)
    report = args.report or args.manifest.with_name(args.manifest.stem + "_report.json")
    _atomic_json(report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "success" else 1)


if __name__ == "__main__":
    main()
