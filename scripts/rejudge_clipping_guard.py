"""A chain preset's guard record judged again under R17's guard as it is now, from the record's
measurements (after a change to the guard, not to the preset). See
processor_harness/clipping_guard_cli.py.

    uv run python scripts/rejudge_clipping_guard.py \
        scripts/processor_harness/presets/limiter/chain-r17.toml
"""

from processor_harness.clipping_guard_cli import rejudge_main

if __name__ == "__main__":
    raise SystemExit(rejudge_main())
