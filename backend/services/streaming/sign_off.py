"""The sign-off clip's storage ports (D26; PG3).

Only the folder, the ports bundle and the key the clip is stored under land here in PR G1's
Task 2: the upload and removal paths (``save_sign_off``, ``remove_sign_off``) are PR G1's
Task 3.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.domain.streaming import ProbedClip
from backend.repositories.user_settings import UserSettingRepository

SIGN_OFF_KEY = "stream_sign_off"
"""The user setting the clip is stored under; written only by the sign-off's own path (PG2)."""

type ClipProbe = Callable[[Path], ProbedClip]
"""Reads a staged file's audio content and reports its format and length (I2)."""


def sign_off_folder(work_dir: Path) -> Path:
    """Where sign-off clips are stored, under the streaming work directory."""
    return work_dir / "sign-off"


@dataclass(frozen=True)
class SignOffPorts:
    """What the sign-off's read and write paths are wired to at the composition root."""

    settings: UserSettingRepository
    folder: Path
    probe: ClipProbe
    commit: Callable[[], None]
