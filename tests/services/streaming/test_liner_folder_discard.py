"""``discard_station_liners`` is best effort (LR3; final review T5-a): it runs after the station
delete is committed, so even a failed check for the folder is logged, never raised.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from structlog.testing import capture_logs

from backend.services.streaming.liners import (
    discard_station_liners,
    liner_folder,
    station_liner_folder,
)

STATION_ID = UUID("6d1c3c0e-0000-4000-8000-000000000001")


def test_a_failed_existence_check_is_logged_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: Path, *args: object, **kwargs: object) -> bool:
        raise PermissionError("access denied")

    monkeypatch.setattr(Path, "exists", refuse)
    with capture_logs() as logs:
        discard_station_liners(liner_folder(tmp_path), STATION_ID)

    assert [e["event"] for e in logs] == ["stream_liner_cleanup_failed"]


def test_a_missing_folder_is_not_an_error(tmp_path: Path) -> None:
    with capture_logs() as logs:
        discard_station_liners(liner_folder(tmp_path), STATION_ID)

    assert logs == []


def test_the_station_folder_is_removed(tmp_path: Path) -> None:
    folder = station_liner_folder(tmp_path, STATION_ID)
    folder.mkdir(parents=True)
    (folder / "a.wav").write_bytes(b"x")

    discard_station_liners(liner_folder(tmp_path), STATION_ID)

    assert not folder.exists()
