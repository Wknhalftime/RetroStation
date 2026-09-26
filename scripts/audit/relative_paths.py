"""Rewrite the filenames in a ruff JSON report as repo-relative POSIX paths.

ruff reports absolute paths, so the same findings read differently from a worktree
and from the main checkout. Audits are committed and diffed as the trend report;
relative paths keep that diff down to real changes.

Usage (run from the repo root, after ruff writes the report):
    uv run python scripts/audit/relative_paths.py audit/ruff.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def relativize(findings: list[dict[str, object]], root: Path) -> list[dict[str, object]]:
    """Return findings whose `filename` is relative to `root`; paths outside it stay as-is."""
    root = root.resolve()
    result = []
    for finding in findings:
        path = Path(str(finding["filename"])).resolve()
        if path.is_relative_to(root):
            finding = {**finding, "filename": path.relative_to(root).as_posix()}
        result.append(finding)
    return result


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "report", type=Path, help="ruff --output-format json report, rewritten in place"
    )
    args = ap.parse_args(argv)

    findings = json.loads(args.report.read_text(encoding="utf-8"))
    rewritten = relativize(findings, Path.cwd())
    args.report.write_text(json.dumps(rewritten, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
