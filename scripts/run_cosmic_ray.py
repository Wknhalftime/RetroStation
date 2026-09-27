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

Generates a session file in audit/cosmic-ray-{sanitized_module}.json.
Exit code: 0 if all mutations complete and survivors <= max_survivors, 1 otherwise.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path


def sanitize_name(path: str) -> str:
    """Convert module path to a filesystem-safe name."""
    return re.sub(r"[/\\.]", "_", path)


def run_baseline_test(test_command: list[str]) -> bool:
    """Run test command on unmutated code to validate it works.

    Returns True if tests pass (exit 0), False otherwise.
    """
    print("=" * 70)
    print("Baseline check: running tests on unmutated code...")
    print("=" * 70)
    result = subprocess.run(test_command, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)

    if result.returncode != 0:
        print("ERROR: Baseline test command failed on unmutated code!", file=sys.stderr)
        print("Fix the test command before running mutation testing.", file=sys.stderr)
        return False
    print("OK: Baseline check passed\n")
    return True


def parse_session_stats_from_report(report_output: str) -> dict[str, int]:
    """Parse mutation results from cr-report text output.

    Returns dict with keys: mutants, killed, survived, timeout, incompetent, pending
    Only includes counts for actually measured outcomes.
    """
    counts = {
        "mutants": 0,
        "killed": 0,
        "survived": 0,
        "timeout": 0,
        "incompetent": 0,
        "pending": 0,
    }

    # Parse cr-report summary lines
    for line in report_output.split("\n"):
        line = line.strip()
        if line.startswith("total jobs:"):
            parts = line.split(":")
            if len(parts) > 1:
                counts["mutants"] = int(parts[1].strip())
        elif line.startswith("surviving mutants:"):
            parts = line.split(":")
            if len(parts) > 1:
                num_str = parts[1].strip().split()[0]
                counts["survived"] = int(num_str)

    # Calculate killed
    counts["killed"] = counts["mutants"] - counts["survived"]

    return counts


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
                print(
                    f"Error: --timeout must be an integer, got {sys.argv[i]}",
                    file=sys.stderr,
                )
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
                    f"Error: --max-survivors must be an integer, got {sys.argv[i]}",
                    file=sys.stderr,
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
        # Test path may include test selectors (::test_name), so check base path only
        test_base = test_path.split("::")[0]
        test_module = Path(test_base)
        if not test_module.exists():
            print(f"Error: Test module not found: {test_base}", file=sys.stderr)
            return 1

    # Generate session filename from sanitized module path
    safe_name = sanitize_name(module_path)
    session_file = Path(f"audit/cosmic-ray-{safe_name}.json")

    # Delete old session to avoid stale results
    session_file.unlink(missing_ok=True)

    # Build test command: keep xdist loaded, disable with -n 0
    test_command = [
        "pytest",
        "-n",
        "0",
        "-p",
        "no:randomly",
        "-x",
        "-q",
        "--tb=no",
    ] + test_paths

    # Run baseline check first
    if not run_baseline_test(test_command):
        return 1

    # Create a temporary cosmic-ray config
    test_command_str = " ".join(test_command)

    config_content = f"""[cosmic-ray]
module-path = "{module_path}"
test-command = "{test_command_str}"
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

        # Parse session stats from cr-report output
        stats = parse_session_stats_from_report(report_result.stdout)

        if not stats:
            print("Error: Could not parse mutation results", file=sys.stderr)
            return 1

        # Build summary line - only report what we measured
        summary_parts = [f"mutants={stats['mutants']}"]
        if stats["killed"] > 0:
            summary_parts.append(f"killed={stats['killed']}")
        if stats["survived"] > 0:
            summary_parts.append(f"survived={stats['survived']}")
        if stats["timeout"] > 0:
            summary_parts.append(f"timeout={stats['timeout']}")
        if stats["incompetent"] > 0:
            summary_parts.append(f"incompetent={stats['incompetent']}")
        if stats["pending"] > 0:
            summary_parts.append(f"pending={stats['pending']}")

        summary_line = " ".join(summary_parts)

        print()
        print("=" * 70)
        print(summary_line)

        # Check for pending mutations (incomplete runs)
        if stats["pending"] > 0:
            print(
                f"FAIL: {stats['pending']} mutations did not complete",
                file=sys.stderr,
            )
            return 1

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
