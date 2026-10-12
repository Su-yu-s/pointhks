"""Load and validate LitePT inference profiles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_FILE = ROOT / "configs" / "inference_profiles.json"
SUPPORTED_TASKS = {"instance_segmentation", "semantic_segmentation"}


@dataclass(frozen=True)
class InferenceProfile:
    name: str
    description: str
    task: str
    entrypoint: Path
    config: Path
    weight: Path
    class_count: int
    domain: str
    input: dict[str, Any]
    source_file: Path


def _resolve(value: str, source_file: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    # Built-in paths are repository-relative. External profiles are relative to
    # the profile file, which makes a custom model bundle portable.
    base = ROOT if source_file.resolve() == DEFAULT_PROFILE_FILE.resolve() else source_file.parent
    return (base / path).resolve()


def load_profiles(path: Path = DEFAULT_PROFILE_FILE) -> dict[str, InferenceProfile]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Inference profile file not found: {path}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in profile file {path}: {exc}") from exc
    if document.get("schema_version") != 1:
        raise ValueError(f"Unsupported profile schema_version in {path}; expected 1.")
    records = document.get("profiles")
    if not isinstance(records, dict) or not records:
        raise ValueError(f"Profile file {path} must contain a non-empty 'profiles' object.")

    profiles: dict[str, InferenceProfile] = {}
    required = {
        "description", "task", "entrypoint", "config", "weight",
        "class_count", "domain", "input",
    }
    for name, record in records.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(record, dict):
            raise ValueError(f"Invalid profile entry in {path}.")
        missing = sorted(required - record.keys())
        if missing:
            raise ValueError(f"Profile '{name}' is missing: {', '.join(missing)}")
        task = record["task"]
        if task not in SUPPORTED_TASKS:
            raise ValueError(
                f"Profile '{name}' has unsupported task '{task}'. "
                f"Supported tasks: {', '.join(sorted(SUPPORTED_TASKS))}."
            )
        class_count = record["class_count"]
        if not isinstance(class_count, int) or class_count < 1:
            raise ValueError(f"Profile '{name}' class_count must be a positive integer.")
        input_spec = record["input"]
        if not isinstance(input_spec, dict) or not input_spec.get("formats"):
            raise ValueError(f"Profile '{name}' input.formats must be a non-empty list.")
        profiles[name] = InferenceProfile(
            name=name,
            description=str(record["description"]),
            task=task,
            entrypoint=_resolve(str(record["entrypoint"]), path),
            config=_resolve(str(record["config"]), path),
            weight=_resolve(str(record["weight"]), path),
            class_count=class_count,
            domain=str(record["domain"]),
            input=input_spec,
            source_file=path,
        )
    return profiles


def get_profile(name: str, path: Path = DEFAULT_PROFILE_FILE) -> InferenceProfile:
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError as exc:
        available = ", ".join(sorted(profiles))
        raise ValueError(f"Unknown profile '{name}'. Available profiles: {available}") from exc


def validate_profile_files(profile: InferenceProfile) -> list[str]:
    problems = []
    for label, path in (
        ("entrypoint", profile.entrypoint),
        ("config", profile.config),
        ("weight", profile.weight),
    ):
        if not path.is_file():
            problems.append(f"{label} not found: {path}")
    return problems
