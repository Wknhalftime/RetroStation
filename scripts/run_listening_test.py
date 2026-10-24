"""Blind listening tests (ABX and preference) between two presets of a harness run, so ear checks
are recorded beside the measurements. See processor_harness/listening_cli.py for the commands.

    uv run python scripts/run_listening_test.py prepare --run var/harness/<run> \\
        --clips <clips.toml> --a <preset A.toml> --b <preset B.toml> --name <test>
    uv run python scripts/run_listening_test.py run var/harness/<run>/listening/<test> --mode abx
    uv run python scripts/run_listening_test.py score var/harness/<run>/listening/<test>
"""

from processor_harness.listening_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
