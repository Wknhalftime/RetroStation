"""Characterisation, artist model phase 0a (SPEC S1): a work merge, and what it does to the lines
of both works.

Phase 3b rebuilds the merge on purpose (AUD-R032, C58: one merge path that pins every line
whose playing file would change, puts a hand-picked master first and redirects merged ids) and
relocks this file. Until then it records today's merge (POST /library/works/{target}/merge),
read only through ``play_file_resolution``:

- the merged work keeps the target's master, re-picked automatically from every file it now
  holds, so the source's hand-picked master is dropped and the source's lines move with it;
- the source's format overrides move to the merged work, except one whose format the target
  already overrides: the target's wins.

The library comes from the scan; the masters and overrides from the works routes; stations,
lines and plays from the locked ``stream_seed``.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.tasks.huey_app import huey
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan, put
from tests.integration.stream_seed import at
from tests.lifespan_client import lifespan_client

type Conn = psycopg.Connection[DictRow]

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="module", autouse=True)
def queue_in_memory() -> Iterator[None]:
    """The hand-offs the writes queue land in memory, never in a worker's queue file (from the
    main checkout, a -n 0 run's queue file is the dev worker's)."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey, "storage", MemoryStorage(huey.name))
    yield
    patch.undo()


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; the token is bypassed."""
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", _migrated_db_url)
    patch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    from backend.dependencies import get_current_token
    from backend.main import app

    async def _token() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _token
    try:
        with lifespan_client(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        patch.undo()


@pytest.fixture
def client(app_client: TestClient, migrated_db: str) -> TestClient:
    """The module's client; tables are truncated before each test."""
    return app_client


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def _tagged(dest: Path, artist: str, title: str, release_type: str) -> None:
    """Master scores: MP3 3 plus album 80, single 60, compilation 40."""
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Release Type", text=[release_type]))
    tags.save()


def _file_named(conn: Conn, name: str) -> UUID:
    rows = conn.execute("SELECT id, file_path FROM library_files").fetchall()
    [file_id] = [UUID(str(r["id"])) for r in rows if PureWindowsPath(r["file_path"]).name == name]
    return file_id


def _work_of(conn: Conn, file_id: UUID) -> str:
    """The id of the work a file is on, to address the works routes; never asserted on."""
    row = conn.execute("SELECT work_id FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None and row["work_id"] is not None
    return str(row["work_id"])


def _plays(conn: Conn, event: UUID) -> UUID | None:
    """The file *event* resolves to, read only through the view; None when nothing plays."""
    [row] = conn.execute(
        "SELECT file_id FROM play_file_resolution WHERE play_event_id = %s", (event,)
    ).fetchall()
    return None if row["file_id"] is None else UUID(str(row["file_id"]))


def _plays_at(conn: Conn, stations: dict[str, UUID], file_id: UUID, title: str) -> list[UUID]:
    """One line, auto-matched to *file_id*, played once at each station; the plays in order."""
    line = seed.identity(conn, title=title, artist="JANET")
    seed.match(conn, line, file_id)
    return [seed.play(conn, seed.playlist(conn, s), line, at("08:00")) for s in stations.values()]


def _override(client: TestClient, work: str, format_name: str, file_id: UUID) -> None:
    response = client.post(
        f"/api/v1/library/works/{work}/format-overrides",
        json={"format_name": format_name, "preferred_file_id": str(file_id)},
    )
    assert response.status_code == 201


def _set_master(client: TestClient, work: str, file_id: UUID) -> None:
    response = client.put(
        f"/api/v1/library/works/{work}/master", json={"preferred_file_id": str(file_id)}
    )
    assert response.status_code == 200


def _merge(client: TestClient, target: str, source: str) -> None:
    response = client.post(
        f"/api/v1/library/works/{target}/merge", json={"source_work_ids": [source]}
    )
    assert response.status_code == 200


def test_merge_drops_the_source_masters_and_moves_its_overrides(
    conn: Conn, client: TestClient, tmp_path: Path
) -> None:
    """S1, a work merge: AGAIN filed under Janet Jackson (target) and under Janet (source).

    The source's hand-picked master (the single) loses to the target's automatic one (the
    album scores higher), so the source's CHR line moves to the album; its AC override moves
    to the merged work and now reaches the target's line at the AC station too.
    """
    root = tmp_path / "library"
    _tagged(root / "again-album.mp3", "Janet Jackson", "Again", "album")
    _tagged(root / "again-single.mp3", "Janet", "Again", "single")
    _tagged(root / "again-ac.mp3", "Janet", "Again (AC Mix)", "compilation")
    full_scan(conn, root)
    album, single, ac_mix = (_file_named(conn, f"again-{n}.mp3") for n in ("album", "single", "ac"))
    target, source = _work_of(conn, album), _work_of(conn, single)
    _set_master(client, source, single)
    _override(client, source, "AC", ac_mix)
    stations = {"KAAA": seed.station(conn, "AC"), "KBBB": seed.station(conn, "CHR")}
    target_line = _plays_at(conn, stations, album, "AGAIN")
    source_line = _plays_at(conn, stations, single, "AGAIN (SINGLE)")
    conn.commit()
    assert [_plays(conn, e) for e in target_line] == [album, album]
    assert [_plays(conn, e) for e in source_line] == [ac_mix, single]

    _merge(client, target, source)

    assert [_plays(conn, e) for e in target_line] == [ac_mix, album]
    assert [_plays(conn, e) for e in source_line] == [ac_mix, album]


def test_merge_keeps_the_targets_override_for_a_shared_format(
    conn: Conn, client: TestClient, tmp_path: Path
) -> None:
    """S1, a work merge where both works override AC: the target's override wins, so the
    source's AC line moves to the target's AC edit."""
    root = tmp_path / "library"
    _tagged(root / "jj-album.mp3", "Janet Jackson", "Again", "album")
    _tagged(root / "jj-ac.mp3", "Janet Jackson", "Again (AC Edit)", "single")
    _tagged(root / "j-album.mp3", "Janet", "Again", "album")
    _tagged(root / "j-ac.mp3", "Janet", "Again (AC Mix)", "compilation")
    full_scan(conn, root)
    jj_album, jj_ac, j_album, j_ac = (
        _file_named(conn, f"{n}.mp3") for n in ("jj-album", "jj-ac", "j-album", "j-ac")
    )
    target, source = _work_of(conn, jj_album), _work_of(conn, j_album)
    _override(client, target, "AC", jj_ac)
    _override(client, source, "AC", j_ac)
    stations = {"KAAA": seed.station(conn, "AC")}
    [target_at_kaaa] = _plays_at(conn, stations, jj_album, "AGAIN")
    [source_at_kaaa] = _plays_at(conn, stations, j_album, "AGAIN (J)")
    conn.commit()
    assert (_plays(conn, target_at_kaaa), _plays(conn, source_at_kaaa)) == (jj_ac, j_ac)

    _merge(client, target, source)

    assert (_plays(conn, target_at_kaaa), _plays(conn, source_at_kaaa)) == (jj_ac, jj_ac)
