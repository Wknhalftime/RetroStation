"""The engine never runs autocue (spec: D78, "The engine never runs autocue (supersedes D49
and the on-demand half of D48, D58, D60)"). Neither the shared settings nor any autocue
function nor enable_autocue_metadata may appear in the session script, nor in any script it
includes, however deeply (review M2: an include of cue_analysis.liq would bring autocue in).
Comments are ignored, so a note about D78 in a script is allowed (audit SF-7)."""

from __future__ import annotations

import re
from pathlib import Path

from backend.playout.liquidsoap_process import SESSION_SCRIPT

INCLUDE = re.compile(r'^\s*%include\s+"([^"]+)"', re.MULTILINE)


def with_includes(script: Path) -> list[Path]:
    """``script`` and every script it includes, transitively, each once."""
    found: list[Path] = []
    pending = [script]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.append(path)
        text = path.read_text(encoding="utf-8")
        pending.extend(path.parent / name for name in INCLUDE.findall(text))
    return found


def test_the_engine_never_runs_autocue() -> None:
    for path in with_includes(SESSION_SCRIPT):
        lines = path.read_text(encoding="utf-8").lower().splitlines()
        code = "\n".join(line.split("#", 1)[0] for line in lines)
        assert "autocue" not in code, path
