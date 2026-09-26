"""Hotspot and change-coupling report: git churn x cyclomatic complexity.

Evidence base: churn predicts defects better than static metrics alone
(Nagappan & Ball 2005); hotspots = change frequency x complexity (Tornhill).

Complexity comes from ruff's C901 rule with the threshold forced to 0, so no
extra dependency is needed. Output is JSON so the next audit can diff it.
Commits listed in .git-blame-ignore-revs (formatting sweeps) are not counted.

Usage:
    uv run python scripts/audit/hotspots.py --since 2026-01-01 --out audit/hotspots.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

C901_MSG = re.compile(r"`(?P<name>[^`]+)` is too complex \((?P<ccn>\d+) > \d+\)")


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode not in (0, 1):  # ruff exits 1 when it reports findings
        raise RuntimeError(f"{' '.join(cmd)} failed ({result.returncode}): {result.stderr}")
    return result.stdout


def ignored_revs(path: Path) -> set[str]:
    """Commits git blame skips (formatting sweeps); churn skips them too."""
    if not path.exists():
        return set()
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return {line.split()[0] for line in lines if line and not line.startswith("#")}


def parse_log(out: str, skip: set[str]) -> tuple[list[list[str]], int]:
    """Split `git log --name-only --format=@@commit %H` output into per-commit .py files.

    Returns the kept commits and how many were dropped for being in `skip`
    (full or abbreviated hashes).
    """
    commits: list[list[str]] = []
    skipped = 0
    for block in out.split("@@commit"):
        tokens = block.split()
        if not tokens:
            continue
        sha, names = tokens[0], tokens[1:]
        if any(sha.startswith(rev) for rev in skip):
            skipped += 1
            continue
        files = [f for f in names if f.endswith(".py")]
        if files:
            commits.append(files)
    return commits, skipped


def churn_and_commits(
    since: str, path: str, skip: set[str]
) -> tuple[Counter[str], list[list[str]], int]:
    out = run(
        [
            "git",
            "log",
            f"--since={since}",
            "--no-merges",
            "--name-only",
            "--format=format:@@commit %H",
            "--",
            path,
        ]
    )
    commits, skipped = parse_log(out, skip)
    churn = Counter(f for files in commits for f in set(files))
    return churn, commits, skipped


def complexity(path: str) -> dict[str, dict[str, object]]:
    out = run(
        [
            "ruff",
            "check",
            path,
            "--select",
            "C901",
            "--output-format",
            "json",
            "--config",
            "lint.mccabe.max-complexity=0",
            "--no-cache",
            "--exit-zero",
        ]
    )
    per_file: dict[str, dict[str, object]] = {}
    root = Path.cwd().resolve()
    for item in json.loads(out or "[]"):
        m = C901_MSG.search(item["message"])
        if not m:
            continue
        rel = Path(item["filename"]).resolve().relative_to(root).as_posix()
        entry = per_file.setdefault(rel, {"total_ccn": 0, "max_ccn": 0, "worst": ""})
        ccn = int(m["ccn"])
        entry["total_ccn"] = int(entry["total_ccn"]) + ccn  # type: ignore[call-overload]
        if ccn > int(entry["max_ccn"]):  # type: ignore[call-overload]
            entry["max_ccn"] = ccn
            entry["worst"] = f"{m['name']} (line {item['location']['row']})"
    return per_file


def coupling(
    commits: list[list[str]], churn: Counter[str], min_shared: int
) -> list[dict[str, object]]:
    pairs: Counter[tuple[str, str]] = Counter()
    for files in commits:
        if len(files) > 30:  # skip sweeping commits (renames, formatting)
            continue
        pairs.update(itertools.combinations(sorted(set(files)), 2))
    rows = []
    for (a, b), shared in pairs.items():
        if shared < min_shared:
            continue
        degree = shared / min(churn[a], churn[b])
        rows.append({"a": a, "b": b, "shared_commits": shared, "degree": round(degree, 2)})
    return sorted(rows, key=lambda r: (-float(r["degree"]), -int(r["shared_commits"])))  # type: ignore[arg-type]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="12 months ago")
    ap.add_argument("--path", default="backend")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-shared", type=int, default=4)
    ap.add_argument("--out", type=Path, default=Path("audit/hotspots.json"))
    ap.add_argument("--ignore-revs-file", type=Path, default=Path(".git-blame-ignore-revs"))
    args = ap.parse_args()

    churn, commits, skipped = churn_and_commits(
        args.since, args.path, ignored_revs(args.ignore_revs_file)
    )
    cx = complexity(args.path)
    hotspots = []
    for f, n in churn.items():
        if not Path(f).exists():  # deleted or moved since
            continue
        c = cx.get(f, {"total_ccn": 0, "max_ccn": 0, "worst": ""})
        hotspots.append({"file": f, "changes": n, **c, "score": n * int(c["total_ccn"])})  # type: ignore[call-overload]
    hotspots.sort(key=lambda h: -int(h["score"]))

    report = {
        "since": args.since,
        "commits": len(commits),
        "skipped_format_commits": skipped,
        "hotspots": hotspots[: args.top],
        "coupling": coupling(commits, churn, args.min_shared)[: args.top],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"{'score':>6} {'chg':>4} {'ccn':>5} {'max':>4}  file / worst function")
    for h in report["hotspots"]:
        print(
            f"{h['score']:>6} {h['changes']:>4} {h['total_ccn']:>5} {h['max_ccn']:>4}  "
            f"{h['file']}  [{h['worst']}]"
        )
    print("\nchange coupling (degree = shared / min(changes)):")
    for r in report["coupling"]:
        print(f"  {r['degree']:.2f} ({r['shared_commits']}x)  {r['a']}  <->  {r['b']}")


if __name__ == "__main__":
    main()
