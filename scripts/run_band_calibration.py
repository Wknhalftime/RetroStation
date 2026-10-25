"""Per-band thresholds for a target mean gain reduction (the band step's starting thresholds).
See processor_harness/calibrate_cli.py.

    uv run python scripts/run_band_calibration.py <preset.toml>... \
        --inputs <programme WAVs>... --target-gr 3.0
"""

from processor_harness.calibrate_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
