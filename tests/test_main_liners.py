"""The composition root points the stream service at the liners folder the upload writes
(PR L2, Task 9; LR3: liners are stored at ``<work_dir>/liners/<station_id>/``, plan note 4).

The reader half of the wiring needs PostgreSQL, so it is in
``tests/integration/test_playable_liners_pg.py::test_main_wires_the_liner_reader``. This test
reads public seams only: ``main.stream_service_config`` and ``liner_folder``.
"""

from __future__ import annotations

from pathlib import Path

from backend.config import Settings
from backend.main import stream_service_config
from backend.services.streaming.liners import liner_folder


def test_main_wires_liner_reader_and_folder(tmp_path: Path) -> None:
    work_dir = tmp_path / "stream-work"
    settings = Settings(_env_file=None, stream_work_dir=work_dir)  # type: ignore[call-arg]

    config = stream_service_config(settings, tmp_path / "logs")

    assert config.liners_dir == liner_folder(work_dir) == work_dir / "liners"
