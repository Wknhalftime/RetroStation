"""The harness lives in scripts/ (not a package), so its directory goes on sys.path here and
the tests import it as ``processor_harness``, the way the CLI entry point does."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
