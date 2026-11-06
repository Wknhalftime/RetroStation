"""The sweep's task module imports first (phase 2a S14).

``gate.py run-task`` imports a task by its dotted path, with nothing loaded before it. A scan
module that imported the sweep at its top would meet this module half loaded, through
``huey_app``'s imports, and the gate could not run the task. A new interpreter, because this
process has already imported these modules.
"""

from __future__ import annotations

import subprocess
import sys


def test_the_module_imports_with_nothing_loaded_before_it() -> None:
    done = subprocess.run(
        [sys.executable, "-c", "import backend.tasks.artist_identity_tasks"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr
