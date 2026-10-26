"""The limiter's gain reduction per input, each variant against the no-limiter reference.
See processor_harness/limiter_gr_cli.py.

    uv run python scripts/run_limiter_gr.py --reference var/harness/<run>/<off>-v1 \
        var/harness/<run>/<variant>-v1 ...
"""

from processor_harness.limiter_gr_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
