#!/usr/bin/env python3
"""Wrapper script for cosmic-ray mutation testing during refactoring.

Usage:
    uv run python scripts/run_cosmic_ray.py backend/services/matching_utils.py \
        tests/services/test_matching_utils.py

Generates a session file in audit/cosmic-ray-{module_hash}.json to track
mutation results. Report is printed to stdout.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    """Run cosmic-ray on a module and report results."""
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    module_path = sys.argv[1]
    test_paths = sys.argv[2:]

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

    # Generate a session filename based on the module path
    module_hash = hashlib.md5(module_path.encode()).hexdigest()[:8]
    session_file = Path(f"audit/cosmic-ray-{module_hash}.json")

    # Create a temporary cosmic-ray config
    test_command = f"pytest -p no:xdist -p no:randomly -n 0 -x --tb=short {' '.join(test_paths)}"

    config_content = f"""[cosmic-ray]
module-path = "{module_path}"
test-command = "{test_command}"
timeout = 10

[cosmic-ray.execution-engine]
name = "local"

[cosmic-ray.distributor]
name = "local"

[cosmic-ray.filter]
# Include all modules that aren't test modules
exclude-modules = ["test_.*"]
"""

    # Write config to a temporary file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False) as f:
        f.write(config_content)
        config_file = Path(f.name)

    try:
        print(f"Running cosmic-ray on {module_path}...")
        print(f"Test paths: {', '.join(test_paths)}")
        print(f"Session file: {session_file}")
        print()

        # Initialize the session
        init_cmd = [
            "cosmic-ray",
            "init",
            str(config_file),
            str(session_file),
        ]

        print(f"Initializing session: {' '.join(init_cmd)}")
        init_result = subprocess.run(init_cmd, capture_output=True, text=True)
        if init_result.returncode != 0:
            print("Error initializing session:")
            print(init_result.stdout)
            print(init_result.stderr)
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
            print(exec_result.stderr)

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

        # Extract summary stats
        print()
        print("=" * 70)
        if "work items" in report_result.stdout or "executed" in report_result.stdout:
            print("Session complete. Mutation testing summary available above.")

        return 0

    finally:
        # Clean up temporary config file
        config_file.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
