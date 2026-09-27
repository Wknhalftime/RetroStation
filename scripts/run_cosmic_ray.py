#!/usr/bin/env python3
"""Wrapper script for cosmic-ray mutation testing during refactoring.

Usage:
    uv run python scripts/run_cosmic_ray.py [OPTIONS] MODULE TEST_PATHS...

Options:
    --timeout SECONDS       Per-mutant timeout (default: 10)
    --max-survivors N       Fail if survivors exceed N (default: no limit, report only)

Example:
    uv run python scripts/run_cosmic_ray.py \\
        backend/services/matching_utils.py \\
        tests/services/test_matching_utils.py

    uv run python scripts/run_cosmic_ray.py --timeout 15 --max-survivors 0 \\
        backend/services/matching_utils.py \\
        tests/services/test_matching_utils.py

Generates a session file in audit/cosmic-ray-{sanitized_module}.json.
Exit code: 0 if survivors <= max_survivors, 1 otherwise.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def sanitize_name(path: str) -> str:
    """Convert module path to a filesystem-safe name."""
    return re.sub(r"[/\\.]", "_", path)


def parse_session_stats(session_file: Path) -> dict[str, int]:
    """Extract mutation counts from the cosmic-ray session database."""
    try:
        with open(session_file) as f:
            session_data = json.load(f)

        # Count outcomes from the work items
        counts = {
            "mutants": 0,
            "killed": 0,
            "survived": 0,
            "timeout": 0,
            "incompetent": 0,
        }

        if "work_items" in session_data:
            for item in session_data["work_items"]:
                counts["mutants"] += 1
                outcome = item.get("outcome")
                if outcome == "killed":
                    counts["killed"] += 1
                elif outcome == "survived":
                    counts["survived"] += 1
                elif outcome == "timeout":
                    counts["timeout"] += 1
                elif outcome == "incompetent":
                    counts["incompetent"] += 1

        return counts
    except (json.JSONDecodeError, KeyError, FileNotFoundError):
        return {"mutants": 0, "killed": 0, "survived": 0, "timeout": 0, "incompetent": 0}


def main() -> int:
    """Run cosmic-ray on a module and report results."""
    # Parse arguments
    timeout = 10
    max_survivors = None
    module_path = None
    test_paths = []

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--timeout":
            i += 1
            if i >= len(sys.argv):
                print("Error: --timeout requires an argument", file=sys.stderr)
                return 1
            try:
                timeout = int(sys.argv[i])
            except ValueError:
                print(f"Error: --timeout must be an integer, got {sys.argv[i]}", file=sys.stderr)
                return 1
        elif arg == "--max-survivors":
            i += 1
            if i >= len(sys.argv):
                print("Error: --max-survivors requires an argument", file=sys.stderr)
                return 1
            try:
                max_survivors = int(sys.argv[i])
            except ValueError:
                print(
                    f"Error: --max-survivors must be an integer, got {sys.argv[i]}", file=sys.stderr
                )
                return 1
        elif not arg.startswith("-"):
            if module_path is None:
                module_path = arg
            else:
                test_paths.append(arg)
        i += 1

    if module_path is None or not test_paths:
        print(__doc__)
        return 1

    # Validate inputs
    module = Path(module_path)
    if not module.exists():
        print(f"Error: Module not found: {module_path}", file=sys.stderr)
        return 1

    for test_path in test_paths:
        test_module = Path(test_path)
        if not test_module.exists():
            print(f"Error: Test module not found: {test_path}", file=sys.stderr)
            return 1

    # Generate session filename from sanitized module path
    safe_name = sanitize_name(module_path)
    session_file = Path(f"audit/cosmic-ray-{safe_name}.json")

    # Delete old session to avoid stale results
    session_file.unlink(missing_ok=True)

    # Create a temporary cosmic-ray config
    test_command = f"pytest -p no:xdist -p no:randomly -n 0 -x --tb=short {' '.join(test_paths)}"

    config_content = f"""[cosmic-ray]
module-path = "{module_path}"
test-command = "{test_command}"
timeout = {timeout}

[cosmic-ray.execution-engine]
name = "local"

[cosmic-ray.distributor]
name = "local"
"""

    # Write config to a temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(config_content)
        config_file = Path(f.name)

    try:
        print(f"Running cosmic-ray on {module_path}...")
        print(f"Test paths: {', '.join(test_paths)}")
        print(f"Session file: {session_file}")
        print(f"Timeout: {timeout}s per mutant")
        if max_survivors is not None:
            print(f"Max survivors: {max_survivors}")
        print()

        # Initialize the session
        init_cmd = [
            "cosmic-ray",
            "init",
            str(config_file),
            str(session_file),
        ]

        print("Initializing session...")
        init_result = subprocess.run(init_cmd, capture_output=True, text=True)
        if init_result.returncode != 0:
            print("Error initializing session:", file=sys.stderr)
            print(init_result.stdout, file=sys.stderr)
            print(init_result.stderr, file=sys.stderr)
            return init_result.returncode

        # Execute mutations
        exec_cmd = [
            "cosmic-ray",
            "exec",
            str(config_file),
            str(session_file),
        ]

        print("Executing mutations...")
        exec_result = subprocess.run(exec_cmd, capture_output=True, text=True)
        print(exec_result.stdout)
        if exec_result.stderr:
            print(exec_result.stderr, file=sys.stderr)

        # Check exec result
        if exec_result.returncode != 0:
            print("Error executing mutations", file=sys.stderr)
            return exec_result.returncode

        # Generate report
        print()
        print("Mutation test report:")
        print("-" * 70)

        report_cmd = [
            "cr-report",
            str(session_file),
        ]

        report_result = subprocess.run(report_cmd, capture_output=True, text=True)
        print(report_result.stdout)

        # Parse session stats and print summary line
        stats = parse_session_stats(session_file)
        summary_line = (
            f"mutants={stats['mutants']} killed={stats['killed']} "
            f"survived={stats['survived']} timeout={stats['timeout']} "
            f"incompetent={stats['incompetent']}"
        )
        print()
        print("=" * 70)
        print(summary_line)

        # Check max survivors
        if max_survivors is not None and stats["survived"] > max_survivors:
            print(
                f"FAIL: {stats['survived']} survivors exceed max of {max_survivors}",
                file=sys.stderr,
            )
            return 1

        return 0

    finally:
        # Clean up temporary config file
        config_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
