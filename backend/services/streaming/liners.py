"""Station liners (LR3, LR4): where each liner's audio lives under the stream work folder.

Liners are stored at ``<work_dir>/liners/<station_id>/<file_name>``. The folder helpers are
pure paths: nothing here creates or reads a file.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

_LINERS_DIR = "liners"


def liner_folder(work_dir: Path) -> Path:
    """The root of every station's liners, under the stream work folder."""
    return work_dir / _LINERS_DIR


def station_liner_folder(work_dir: Path, station_id: UUID) -> Path:
    """The folder holding one station's liner files."""
    return liner_folder(work_dir) / str(station_id)
