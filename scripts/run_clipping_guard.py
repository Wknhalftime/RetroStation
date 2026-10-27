"""R17's clipping guard on one run of a chain preset; writes the preset's guard record. See
processor_harness/clipping_guard_cli.py.

    uv run python scripts/run_clipping_guard.py \
        scripts/processor_harness/presets/limiter/chain-r17.toml var/harness/<run>
"""

from processor_harness.clipping_guard_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
