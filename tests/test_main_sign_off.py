"""The composition root wires one sign-off folder for the upload and the stream service (PR
G1, Task 4; traceability J: T4.11).

Requirements: design note 6 (the stream service reads the user's clip from the folder the
upload writes; production has no default clip, ``final_clip`` is ``None``); H8 (one owner per
fact); M13 (the test reads public seams: ``main.stream_service_config`` and the sign-off
folder dependency, never a private attribute).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.config import Settings, get_settings
from backend.dependencies import get_sign_off_folder
from backend.main import stream_service_config
from backend.services.streaming.sign_off import sign_off_folder


@pytest.fixture
def work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    work = tmp_path / "stream-work"
    monkeypatch.setenv("STREAM_WORK_DIR", str(work))
    get_settings.cache_clear()
    yield work
    get_settings.cache_clear()


def test_the_service_and_the_upload_share_one_sign_off_folder(
    work_dir: Path, tmp_path: Path
) -> None:
    # T4.11: the service's sign_off_dir, the folder rule and the upload's folder agree.
    settings = Settings(_env_file=None, stream_work_dir=work_dir)  # type: ignore[call-arg]
    config = stream_service_config(settings, tmp_path / "logs")
    assert config.sign_off_dir == sign_off_folder(work_dir) == get_sign_off_folder()
    assert sign_off_folder(work_dir) == work_dir / "sign-off"
    assert config.log_dir == tmp_path / "logs"
    assert config.final_clip is None
