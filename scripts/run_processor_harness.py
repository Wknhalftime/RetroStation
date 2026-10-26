"""Measurement harness for the streaming processor (brief R4: measurements set the values).

Renders the corpus (scripts/processor_harness/corpus.toml) through each chain preset and writes
JSON per preset, summary.csv / summary.md (one row per preset) and comparison.csv to --out.

    uv run python scripts/run_processor_harness.py --out var/harness/<run> \\
        scripts/processor_harness/presets/bypass.toml \\
        scripts/processor_harness/presets/live-2026-10-22.toml

Liquidsoap comes from --liquidsoap or LIQUIDSOAP_PATH; FFmpeg from --ffmpeg or PATH.
"""

from processor_harness.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
