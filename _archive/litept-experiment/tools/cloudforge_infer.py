"""Unified command line entry point for released and custom LitePT weights."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from cloudforge_profiles import DEFAULT_PROFILE_FILE, get_profile, load_profiles, validate_profile_files


def parse_args() -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, help="Input point-cloud file.")
    parser.add_argument("--profile", default="scannet200-instances")
    parser.add_argument("--profiles-file", type=Path, default=DEFAULT_PROFILE_FILE)
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--check-profile", action="store_true")
    parser.add_argument("--check-input", action="store_true")
    parser.add_argument("--input-unit", choices=("m", "cm", "mm"), default="m")
    return parser.parse_known_args()


def profile_summary(profile) -> dict:
    return {
        "name": profile.name,
        "task": profile.task,
        "description": profile.description,
        "domain": profile.domain,
        "class_count": profile.class_count,
        "formats": profile.input["formats"],
        "config": str(profile.config),
        "weight": str(profile.weight),
        "ready": not validate_profile_files(profile),
        "problems": validate_profile_files(profile),
    }


def main() -> None:
    args, forwarded = parse_args()
    if args.list_profiles:
        profiles = load_profiles(args.profiles_file)
        print(json.dumps([profile_summary(value) for value in profiles.values()], ensure_ascii=False, indent=2))
        return

    profile = get_profile(args.profile, args.profiles_file)
    problems = validate_profile_files(profile)
    if problems:
        raise RuntimeError("Profile is not ready:\n- " + "\n- ".join(problems))
    if args.check_profile:
        print(json.dumps(profile_summary(profile), ensure_ascii=False, indent=2))
        return
    if args.input is None:
        raise ValueError("An input file is required unless --list-profiles or --check-profile is used.")
    suffix = args.input.suffix.lower().lstrip(".")
    formats = [str(value).lower().lstrip(".") for value in profile.input["formats"]]
    if suffix not in formats:
        raise ValueError(
            f"Profile '{profile.name}' accepts {formats}, but input is '{args.input.suffix or '<none>'}'."
        )
    if args.check_input:
        from cloudforge_check_input import inspect_input

        report = inspect_input(args.input, args.profile, args.profiles_file, args.input_unit)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    command = [
        sys.executable,
        str(profile.entrypoint),
        str(args.input),
        "--config", str(profile.config),
        "--weight", str(profile.weight),
        *forwarded,
    ]
    if "bin" not in profile.input["formats"] and args.input_unit != "m":
        command.extend(("--input-unit", args.input_unit))
    completed = subprocess.run(command, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"LitePT error: {exc}", file=sys.stderr)
        raise SystemExit(2)
