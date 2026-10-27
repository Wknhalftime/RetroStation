"""The host's capacity for a chain preset (R18: re-measure on any host change). See
processor_harness/host_load_cli.py.

    uv run python scripts/run_host_load.py \
        scripts/processor_harness/presets/limiter/chain-r17.toml \
        --corpus var/harness/corpus-cpu.toml --out var/harness/<date>-host-load
"""

from processor_harness.host_load_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
