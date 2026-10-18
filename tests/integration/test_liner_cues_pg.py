"""Station liners, PR L1 Task 3: the cue store treats a liner's audio as "in the library",
against PostgreSQL.

Requirement LR4: autocue analyses liners (``store_if_current`` accepts a liner id carrying
the hash) and its daily prune keeps their rows (D56's "in the library" now also means "a
liner carries it"); true orphans are still pruned. Pre-flight ruling (a): the run's liner work
gives each liner's path absolute under ``liner_folder(stream_work_dir)``, checked through the
public cue-analysis task (``stream_cue_analysis_task``), never a private helper.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from structlog.testing import capture_logs

import backend.tasks.stream_cue_tasks as tasks_module
from backend.db.repositories.station_liners import PgStationLinerRepository
from backend.db.repositories.stream_cues import PgStreamCueRepository
from backend.domain.library import AudioHash
from backend.domain.liners import StationLiner
from backend.domain.streaming import CUE_ANALYSER_VERSION, ClipFormat, CueAnalysis, CuePoints
from backend.playout.cue_analysis import BatchAnalysis, CueFile
from backend.services.streaming.liners import station_liner_folder
from backend.tasks.stream_cue_tasks import FailureMemory
from tests.integration import stream_seed as seed
from tests.services.streaming.cue_library import FakeAnalyser

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

T0 = datetime(2026, 10, 18, 4, 0, tzinfo=UTC)
GRACE = timedelta(hours=24)
POINTS = CuePoints(120, 6_900, 0, 400, 300, -3.5)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, autocommit=True, row_factory=dict_row) as c:
        yield c


def new_hash() -> AudioHash:
    return AudioHash.parse(f"flac-md5:{uuid4().hex}")


def add_liner(conn: Conn, audio: AudioHash, station_id: UUID | None = None) -> StationLiner:
    liner_id = uuid4()
    liner = StationLiner(
        id=liner_id,
        station_id=station_id or seed.station(conn),
        name="WKRP ID",
        file_name=f"{liner_id}.flac",
        format=ClipFormat.FLAC,
        audio_hash=audio,
        duration_ms=7_000,
        markers=None,
        uploaded_at=T0,
    )
    PgStationLinerRepository(conn).add(liner)
    return liner


def analysis(audio: AudioHash) -> CueAnalysis:
    return CueAnalysis(
        audio_hash=audio,
        cues=POINTS,
        loudness_lufs=-14.0,
        analysis_failed=False,
        analyser_version=CUE_ANALYSER_VERSION,
    )


def cue_hashes(conn: Conn) -> set[str]:
    return {str(r["audio_hash"]) for r in conn.execute("SELECT audio_hash FROM stream_cues")}


def marks(conn: Conn) -> dict[str, datetime]:
    rows = conn.execute(
        "SELECT audio_hash, orphaned_at FROM stream_cues WHERE orphaned_at IS NOT NULL"
    ).fetchall()
    return {str(r["audio_hash"]): r["orphaned_at"] for r in rows}


def test_store_if_current_accepts_a_liner_id(conn: Conn) -> None:
    """LR4: an analysis made for a liner is stored while that liner carries the hash; no
    library file needs to."""
    audio = new_hash()
    liner = add_liner(conn, audio)

    stored = PgStreamCueRepository(conn).store_if_current(analysis(audio), liner.id)

    assert stored is True
    assert PgStreamCueRepository(conn).get(audio) == analysis(audio)


@pytest.mark.parametrize("change", ["another hash", "liner deleted"])
def test_store_if_current_refuses_a_stale_liner_hash(conn: Conn, change: str) -> None:
    """LR4 (For PR D and PR E, "stores nothing if the hash changed meanwhile"): a liner that
    no longer carries the analysed hash, or is gone, stores nothing."""
    audio = new_hash()
    liner = add_liner(conn, audio)
    if change == "another hash":
        conn.execute(
            "UPDATE station_liners SET audio_hash = %s WHERE id = %s",
            (str(new_hash()), liner.id),
        )
    else:
        conn.execute("DELETE FROM station_liners WHERE id = %s", (liner.id,))

    stored = PgStreamCueRepository(conn).store_if_current(analysis(audio), liner.id)

    assert (stored, cue_hashes(conn)) == (False, set())


def test_prune_keeps_a_liner_only_hash(conn: Conn) -> None:
    """LR4 (D56): audio only a liner carries is in the library: never marked, never pruned."""
    audio = new_hash()
    add_liner(conn, audio)
    store = PgStreamCueRepository(conn)
    store.upsert(analysis(audio))

    store.prune_orphans(T0, GRACE)
    store.prune_orphans(T0 + GRACE, GRACE)

    assert (cue_hashes(conn), marks(conn)) == ({str(audio)}, {})


def test_prune_clears_the_mark_when_a_liner_carries_the_hash(conn: Conn) -> None:
    """LR4 (D56 step 1): a marked row whose audio a liner now carries loses its mark."""
    audio = new_hash()
    store = PgStreamCueRepository(conn)
    store.upsert(analysis(audio))
    store.prune_orphans(T0, GRACE)
    assert marks(conn) == {str(audio): T0}

    add_liner(conn, audio)
    store.prune_orphans(T0 + GRACE, GRACE)

    assert (cue_hashes(conn), marks(conn)) == ({str(audio)}, {})


def test_prune_still_drops_true_orphans(conn: Conn) -> None:
    """LR4, D56 regression: with a liner present, audio nothing carries is still pruned on
    the second strike; the liner's and the library file's rows stay."""
    liner_audio, file_audio, orphan = new_hash(), new_hash(), new_hash()
    add_liner(conn, liner_audio)
    seed.library_file(conn, audio_hash=str(file_audio))
    store = PgStreamCueRepository(conn)
    for audio in (liner_audio, file_audio, orphan):
        store.upsert(analysis(audio))

    store.prune_orphans(T0, GRACE)
    assert marks(conn) == {str(orphan): T0}
    store.prune_orphans(T0 + GRACE, GRACE)

    assert (cue_hashes(conn), marks(conn)) == ({str(liner_audio), str(file_audio)}, {})


def test_a_cue_run_analyses_a_stored_liner(
    conn: Conn, migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LR4, ruling (a), plan note 14: the public cue-analysis task, on real SQL, finds a liner
    needing analysis, opens its file at ``<stream_work_dir>/liners/<station_id>/<file_name>``
    first, and stores its cues under the liner's hash (``store_if_current`` on a liner id).
    Only the analyser and the settings are stand-ins, as in the locked E1 task tests."""
    station_id = seed.station(conn)
    liner = add_liner(conn, new_hash(), station_id)
    path = station_liner_folder(tmp_path, station_id) / liner.file_name
    path.parent.mkdir(parents=True)
    path.write_bytes(b"liner audio")
    analyser = FakeAnalyser()
    settings = SimpleNamespace(
        database_url=migrated_db,
        liquidsoap_path=tmp_path / "liquidsoap.exe",
        stream_work_dir=tmp_path,
        stream_enabled=False,
    )

    def analyse_batch(
        files: list[CueFile], base_env: dict[str, str], config: object
    ) -> BatchAnalysis:
        return analyser(files)

    monkeypatch.setattr(tasks_module, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks_module, "analyse_batch", analyse_batch)
    monkeypatch.setattr(tasks_module, "FAILURES", FailureMemory())
    with capture_logs() as logs:
        tasks_module.stream_cue_analysis_task.call_local()

    assert [e for e in logs if e["event"] == "stream_cue_task_failed"] == []
    assert [Path(p) for p in analyser.order()][:1] == [path]
    stored = PgStreamCueRepository(conn).get(liner.audio_hash)
    assert stored is not None and stored.analysis_failed is False
