"""Station liners, PR L1 Task 5: the liner routes as wired at the composition root, over the
real app and PostgreSQL.

Requirements: LR2, LR3, LR4. The real ``get_liner_ports`` stores an upload in
``station_liner_folder(STREAM_WORK_DIR, station_id)``, the folder the cue task reads liners
from (Task 3), with a ``station_liners`` row; deleting a station removes that folder (Task 5,
``delete_station``) and leaves another station's alone. The fake-backed route tests are in
``test_station_liner_routes.py``.
"""

from __future__ import annotations

import io
import wave
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import DictRow

from backend.config import get_settings
from backend.services.streaming.liners import station_liner_folder

pytestmark = pytest.mark.integration


def wav_bytes(seconds: float) -> bytes:
    """A silent mono 16-bit 8 kHz WAV of exactly ``seconds``."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(8_000)
        out.writeframes(b"\x00\x00" * round(seconds * 8_000))
    return buffer.getvalue()


@pytest.fixture
def work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """``STREAM_WORK_DIR`` for the real app's settings, read at request time."""
    folder = tmp_path / "stream-work"
    monkeypatch.setenv("STREAM_WORK_DIR", str(folder))
    get_settings.cache_clear()
    yield folder
    get_settings.cache_clear()


def new_station(db_conn: psycopg.Connection[DictRow], call_letters: str) -> UUID:
    row = db_conn.execute(
        "INSERT INTO stations (call_letters) VALUES (%s) RETURNING id", (call_letters,)
    ).fetchone()
    db_conn.commit()
    assert row is not None
    return UUID(str(row["id"]))


def test_an_upload_lands_where_the_cue_task_reads_liners(
    client: TestClient, db_conn: psycopg.Connection[DictRow], work: Path
) -> None:
    """LR2, LR3, LR4: the real ports store the upload as ``<id>.wav`` in
    ``station_liner_folder(STREAM_WORK_DIR, station)``, with its row, and list it."""
    station_id = new_station(db_conn, "WKRP")
    data = wav_bytes(2.0)

    response = client.post(
        f"/api/v1/stations/{station_id}/liners",
        files={"file": ("WKRP ID.wav", data, "application/octet-stream")},
    )

    assert (response.status_code, response.json()["outcome"]) == (201, "added")
    liner_id = UUID(response.json()["id"])
    stored = station_liner_folder(work, station_id) / f"{liner_id}.wav"
    assert stored.read_bytes() == data
    row = db_conn.execute(
        "SELECT station_id, name, file_name FROM station_liners WHERE id = %s", (liner_id,)
    ).fetchone()
    assert row is not None
    assert (row["station_id"], row["name"], row["file_name"]) == (
        station_id,
        "WKRP ID",
        f"{liner_id}.wav",
    )
    listed = client.get(f"/api/v1/stations/{station_id}/liners").json()["liners"]
    assert [entry["id"] for entry in listed] == [str(liner_id)]


def test_station_delete_removes_liner_folder(
    client: TestClient, db_conn: psycopg.Connection[DictRow], work: Path
) -> None:
    """LR3: deleting a station removes its liners' folder under the stream work directory;
    another station's folder stays."""
    station_id = new_station(db_conn, "WKRP")
    doomed = station_liner_folder(work, station_id)
    kept = station_liner_folder(work, uuid4())
    for folder in (doomed, kept):
        folder.mkdir(parents=True)
        (folder / f"{uuid4()}.wav").write_bytes(wav_bytes(1.0))

    response = client.delete(f"/api/v1/stations/{station_id}")

    assert response.status_code == 204
    assert not doomed.exists()
    assert len(list(kept.iterdir())) == 1


def test_unknown_station_delete_leaves_folders_alone(client: TestClient, work: Path) -> None:
    """LR3: deleting a station that does not exist is 404 and removes nothing, even a folder
    named after that id."""
    station_id = uuid4()
    folder = station_liner_folder(work, station_id)
    folder.mkdir(parents=True)
    (folder / f"{uuid4()}.wav").write_bytes(wav_bytes(1.0))

    response = client.delete(f"/api/v1/stations/{station_id}")

    assert response.status_code == 404
    assert len(list(folder.iterdir())) == 1
