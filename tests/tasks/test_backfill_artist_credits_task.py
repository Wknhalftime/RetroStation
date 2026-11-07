"""The run-once backfill of artist MBIDs, credits and names (phase 2a S3, S4, S10, S13's fill, S14's
files half, E3, E6; C26, C43, C69, AUD-R028), on PostgreSQL.

It fills ``artist_mbids`` and ``credit`` for every row with ``raw_metadata``, whatever its status,
and re-reads only the MP3s, whose frames lost their separators. When the tags can't name a file's
MBIDs, the names come from ``mb_cache`` (expired or not), else from ``lookup_recordings``. It fills
S10's existing names and S13's stubs, then runs S14's sweep. MusicBrainz down leaves those files
for the next run, with no stub made, and the done-marker waits. Once done, a run does nothing.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from mutagen.id3 import ID3, TIT2, TPE1, TXXX, UFID
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb
from structlog.testing import capture_logs

from backend.config import get_settings
from backend.db.repositories.artist_tag_files import PgArtistTagFilesRepository
from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.musicbrainz_cache import PgMusicBrainzCacheRepository
from backend.domain.library import ARTIST_CREDITS_BACKFILLED_SETTING
from backend.domain.system import MusicBrainzCache
from backend.tasks import artist_credit_backfill_tasks, huey_app
from backend.tasks.artist_credit_backfill_tasks import backfill_artist_credits_task
from tests.fakes.mb_client import FakeMbClient
from tests.fixtures.audio_builders import write_flac, write_mp3
from tests.integration.missing_file_seed import full_scan

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
THREE = "0a000000-0000-4000-8000-000000000003"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
RECORDING = "0b000000-0000-4000-8000-000000000001"
OTHER_RECORDING = "0b000000-0000-4000-8000-000000000002"
NOW = datetime(2026, 11, 6, tzinfo=UTC)
NO_MB_CLIENT = FakeMbClient()


class _Client(FakeMbClient):
    """A fake client the task can open as it opens the real one."""

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection. The settings name the test database, so the task's own connections
    use it, and whatever it hands off stays off the queue."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection
    get_settings.cache_clear()


def _run(
    conn: Conn, monkeypatch: pytest.MonkeyPatch, client: _Client | None = None
) -> dict[str, Any]:
    """One run of the task, on the fake client, with the test's work committed first."""
    conn.commit()
    used = client if client is not None else _Client()
    monkeypatch.setattr(
        artist_credit_backfill_tasks, "MusicBrainzApiClient", lambda *_a, **_k: used
    )
    result: dict[str, Any] = backfill_artist_credits_task.call_local()
    conn.rollback()
    return result


def _vorbis(
    artist: str | None = "A & B", names: list[str] | None = None, mbids: list[str] | None = None
) -> dict[str, str]:
    raw: dict[str, str] = {}
    if artist is not None:
        raw["artist"] = str([artist])
    if names is not None:
        raw["artists"] = str(names)
    if mbids is not None:
        raw["musicbrainz_artistid"] = str(mbids)
    return raw


def _row(
    conn: Conn,
    raw: dict[str, str] | None,
    *,
    path: str | None = None,
    fmt: str = "flac",
    status: str = "present",
    recording: str | None = None,
    artist_name: str = "A & B",
    normalized: str = "a b",
    title: str = "Some Song",
) -> str:
    row = conn.execute(
        """INSERT INTO library_files
               (file_path, format, raw_metadata, file_status, missing_since, recording_mbid,
                artist_name, normalized_artist_name, track_title)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
           RETURNING id""",
        (
            path or f"/music/{uuid4().hex}.{fmt}",
            fmt,
            Jsonb(raw) if raw is not None else None,
            status,
            NOW if status == "missing" else None,
            recording,
            artist_name,
            normalized,
            title,
        ),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _columns(conn: Conn, file_id: str) -> tuple[list[str] | None, list[dict[str, Any]] | None]:
    row = conn.execute(
        "SELECT artist_mbids, credit FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return row["artist_mbids"], row["credit"]


def _cache(conn: Conn, key: str, payload: dict[str, Any], *, expired: bool = False) -> None:
    kind, _, mbid = key.partition(":")
    PgMusicBrainzCacheRepository(conn).set(
        MusicBrainzCache(
            id=uuid4(),
            cache_key=key,
            entity_type=kind,
            entity_mbid=mbid,
            response_data=payload,
            cached_at=NOW,
            expires_at=NOW - timedelta(days=1) if expired else NOW + timedelta(days=365),
        )
    )


def _cached_artist(conn: Conn, mbid: str, name: str, *, expired: bool = False) -> None:
    _cache(conn, f"artist:{mbid}", {"id": mbid, "name": name}, expired=expired)


def _credit(mbid: str, name: str, join: str | None = None) -> dict[str, Any]:
    # A real credit's own name can be a credited-as one: the artist's is what names the MBID.
    credit: dict[str, Any] = {"name": f"credited as {name}", "artist": {"id": mbid, "name": name}}
    if join is not None:
        credit["joinphrase"] = join
    return credit


def _marker(conn: Conn) -> str | None:
    row = conn.execute(
        "SELECT value FROM user_settings WHERE key = %s", (ARTIST_CREDITS_BACKFILLED_SETTING,)
    ).fetchone()
    return None if row is None else str(row["value"])


def _mp3(
    path: Path,
    artist: str,
    mbids: list[str],
    names: list[str] | None = None,
    recording: str | None = None,
) -> Path:
    write_mp3(path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Song"]))
    if recording is not None:
        tags.add(UFID(owner="http://musicbrainz.org", data=recording.encode("ascii")))
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=mbids))
    if names is not None:
        tags.add(TXXX(encoding=3, desc="ARTISTS", text=names))
    tags.save(str(path))
    return path


def _lost_separators(*mbids: str) -> dict[str, str]:
    """What ``raw_metadata`` kept of an MP3's multi-value frame."""
    return {"TPE1": "A & B", "TXXX:MusicBrainz Artist Id": "".join(mbids)}


# --- the import the gate makes --------------------------------------------------------------------


def test_the_task_module_imports_with_nothing_loaded_before_it() -> None:
    """``gate.py run-task`` imports a task by its dotted path."""
    done = subprocess.run(
        [sys.executable, "-c", "import backend.tasks.artist_credit_backfill_tasks"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert done.returncode == 0, done.stderr


# --- filling the columns --------------------------------------------------------------------------


def test_every_row_with_raw_metadata_is_filled_whatever_its_status(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    present = _row(conn, _vorbis("A", mbids=[ONE]))
    missing = _row(conn, _vorbis("A", mbids=[TWO]), status="missing")
    untagged = _row(conn, _vorbis("A"))
    no_tags = _row(conn, {})
    never_read = _row(conn, None)

    result = _run(conn, monkeypatch)

    assert _columns(conn, present) == ([ONE], [{"name": "A", "mbid": ONE, "join": ""}])
    assert _columns(conn, missing) == ([TWO], [{"name": "A", "mbid": TWO, "join": ""}])
    assert _columns(conn, untagged) == ([], None)
    assert _columns(conn, no_tags) == ([], None)
    assert _columns(conn, never_read) == (None, None)
    assert result["files"] == 4


def test_the_name_columns_and_the_scalar_stay_byte_identical(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))
    conn.execute("UPDATE library_files SET artist_mbid = %s WHERE id = %s", (ONE, file_id))
    query = """SELECT artist_name, normalized_artist_name, artist_mbid, raw_metadata, indexed_at,
                      file_status, work_id
                 FROM library_files WHERE id = %s"""
    before = conn.execute(query, (file_id,)).fetchone()
    conn.commit()

    _run(conn, monkeypatch)

    assert conn.execute(query, (file_id,)).fetchone() == before


def test_joined_values_split_on_the_pattern(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    file_id = _row(conn, _vorbis("A & B", ["A", "B"], [f"{ONE}, {TWO.upper()}"]))

    _run(conn, monkeypatch)

    assert _columns(conn, file_id) == (
        [ONE, TWO],
        [{"name": "A", "mbid": ONE, "join": " & "}, {"name": "B", "mbid": TWO, "join": ""}],
    )


def test_an_empty_library_finishes(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _run(conn, monkeypatch)

    assert result == {
        "files": 0,
        "paired": 0,
        "named_from_cache": 0,
        "looked_up": 0,
        "stubbed": 0,
        "absent": 0,
        "waiting": 0,
        "done": True,
    }
    assert _marker(conn) is not None


def test_single_values_ask_nobody(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Empty": no multi-MBID files, so the backfill only copies single values."""
    client = _Client()
    _row(conn, _vorbis("A", mbids=[ONE]))

    _run(conn, monkeypatch, client)

    assert client.calls == []


# --- the MP3s ---------------------------------------------------------------------------------


def test_an_mp3_is_read_from_disk(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _mp3(tmp_path / "a.mp3", "A & B", [ONE, TWO], ["A", "B"])
    file_id = _row(conn, _lost_separators(ONE, TWO), path=str(path), fmt="mp3")

    result = _run(conn, monkeypatch)

    assert _columns(conn, file_id) == (
        [ONE, TWO],
        [{"name": "A", "mbid": ONE, "join": " & "}, {"name": "B", "mbid": TWO, "join": ""}],
    )
    assert result["absent"] == 0


def test_the_mp3_re_read_writes_these_two_columns_only(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file's tags say something else now, and only the next scan may act on that: S2's
    UFID reaches ``recording_mbid`` then, not through this."""
    path = _mp3(tmp_path / "a.mp3", "Someone Else", [ONE], recording=RECORDING)
    file_id = _row(
        conn,
        {"TPE1": "Stored"},
        path=str(path),
        fmt="mp3",
        artist_name="Stored",
        normalized="stored",
    )
    query = """SELECT artist_name, normalized_artist_name, artist_mbid, raw_metadata, indexed_at,
                      recording_mbid, file_size, file_mtime_ns
                 FROM library_files WHERE id = %s"""
    before = conn.execute(query, (file_id,)).fetchone()
    conn.commit()

    _run(conn, monkeypatch)

    assert conn.execute(query, (file_id,)).fetchone() == before
    assert _columns(conn, file_id)[0] == [ONE]


def test_an_absent_mp3_is_skipped_and_counted(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E6: its columns stay NULL, and a scan that finds it again fills them. It doesn't hold the
    marker back: nothing the backfill does would change it."""
    file_id = _row(conn, _lost_separators(ONE, TWO), path=str(tmp_path / "gone.mp3"), fmt="mp3")

    result = _run(conn, monkeypatch)

    assert _columns(conn, file_id) == (None, None)
    assert (result["absent"], result["files"], result["done"]) == (1, 0, True)
    assert PgArtistRepository(conn).get_by_mbid(ONE) is None


def test_an_unreadable_mp3_is_skipped_and_counted(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "broken.mp3"
    path.write_bytes(b"this is not audio")
    file_id = _row(conn, {"TPE1": "A"}, path=str(path), fmt="mp3")

    result = _run(conn, monkeypatch)

    assert _columns(conn, file_id) == (None, None)
    assert result["absent"] == 1


def test_a_flac_is_never_opened(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C26: no disk re-read. The path doesn't exist, and the saved tags are enough."""
    file_id = _row(conn, _vorbis("A", mbids=[ONE]), path=str(tmp_path / "gone.flac"))

    result = _run(conn, monkeypatch)

    assert _columns(conn, file_id)[0] == [ONE]
    assert result["absent"] == 0


# --- naming what the tags can't -------------------------------------------------------------------


def test_tags_that_pair_ask_nobody(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(recordings={RECORDING: {"artist-credit": []}})
    _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert client.calls == []
    assert (result["paired"], result["named_from_cache"], result["looked_up"]) == (1, 0, 0)


def test_names_from_cached_artists(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    _cached_artist(conn, ONE, "A")
    _cached_artist(conn, TWO, "B")
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, file_id)[1] == [
        {"name": "A", "mbid": ONE, "join": " & "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]
    assert client.calls == []
    assert (result["paired"], result["named_from_cache"], result["looked_up"]) == (0, 1, 0)


def test_an_expired_row_still_names(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """S10's cache port ignores ``expires_at``: an old name is still the artist's name."""
    client = _Client()
    _cached_artist(conn, ONE, "A", expired=True)
    _cached_artist(conn, TWO, "B", expired=True)
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)

    _run(conn, monkeypatch, client)

    credit = _columns(conn, file_id)[1]
    assert credit is not None
    assert [entry["name"] for entry in credit] == ["A", "B"]
    assert client.calls == []


def test_names_from_a_cached_credit(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Its joining words too, since this print can't give them."""
    client = _Client()
    _cache(
        conn,
        f"recording:{RECORDING}",
        {"id": RECORDING, "artist-credit": [_credit(ONE, "A", " + "), _credit(TWO, "B", "")]},
    )
    file_id = _row(conn, _vorbis("Printed Otherwise", mbids=[ONE, TWO]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, file_id)[1] == [
        {"name": "A", "mbid": ONE, "join": " + "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]
    assert client.calls == []
    assert result["named_from_cache"] == 1


def test_names_from_a_slim_cached_row(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """``recording-by-mbid`` rows keep the names and no join phrase. A row saying the index has
    no such recording names nothing, and a lookup is still asked."""
    client = _Client()
    _cache(
        conn,
        f"recording-by-mbid:{RECORDING}",
        {"id": RECORDING, "artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]},
    )
    _cache(conn, f"recording-by-mbid:{OTHER_RECORDING}", {"not_in_index": True})
    slim = _row(conn, _vorbis("Other", mbids=[ONE, TWO]), recording=RECORDING)
    gone = _row(conn, _vorbis("Other", mbids=[THREE, TWO]), recording=OTHER_RECORDING)

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, slim)[1] == [
        {"name": "A", "mbid": ONE, "join": None},
        {"name": "B", "mbid": TWO, "join": None},
    ]
    assert client.calls == [f"lookup_recordings:{OTHER_RECORDING}"]
    assert _columns(conn, gone)[0] == [THREE, TWO]
    assert result["named_from_cache"] == 1


def test_a_full_cached_row_beats_a_slim_one(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the full row has the joining words."""
    _cache(
        conn,
        f"recording-by-mbid:{RECORDING}",
        {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]},
    )
    _cache(
        conn,
        f"recording:{RECORDING}",
        {"artist-credit": [_credit(ONE, "A", " + "), _credit(TWO, "B", "")]},
    )
    file_id = _row(conn, _vorbis("Other", mbids=[ONE, TWO]), recording=RECORDING)

    _run(conn, monkeypatch)

    assert _columns(conn, file_id)[1] == [
        {"name": "A", "mbid": ONE, "join": " + "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]


def test_names_from_a_lookup(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """S4: a slim row has no join phrase, so a join only it could give stays null. The print can
    still give one."""
    client = _Client(
        recordings={
            RECORDING: {"id": RECORDING, "artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]}
        }
    )
    printed = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)
    unprinted = _row(conn, _vorbis("Other", mbids=[ONE, TWO]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, printed)[1] == [
        {"name": "A", "mbid": ONE, "join": " & "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]
    assert _columns(conn, unprinted)[1] == [
        {"name": "A", "mbid": ONE, "join": None},
        {"name": "B", "mbid": TWO, "join": None},
    ]
    assert client.calls == [f"lookup_recordings:{RECORDING}"]
    assert result["looked_up"] == 2


def test_the_cache_answers_before_a_lookup(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """One name from the cache, one from the lookup: only what is still unnamed is looked up."""
    client = _Client(recordings={RECORDING: {"artist-credit": [_credit(TWO, "B")]}})
    _cached_artist(conn, ONE, "From The Cache")
    file_id = _row(conn, _vorbis("Other", mbids=[ONE, TWO]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    credit = _columns(conn, file_id)[1]
    assert credit is not None
    assert [entry["name"] for entry in credit] == ["From The Cache", "B"]
    assert (result["named_from_cache"], result["looked_up"]) == (0, 1)


def test_a_file_with_no_recording_mbid_isnt_looked_up(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client()
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]))

    result = _run(conn, monkeypatch, client)

    assert client.calls == []
    assert _columns(conn, file_id)[1] == [
        {"name": None, "mbid": ONE, "join": None},
        {"name": None, "mbid": TWO, "join": None},
    ]
    assert result["stubbed"] == 2


def test_a_recording_musicbrainz_doesnt_know(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 isn't an outage: the names stay null, the stubs are made, and the marker is set."""
    client = _Client()
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, file_id)[0] == [ONE, TWO]
    assert (result["stubbed"], result["waiting"], result["done"]) == (2, 0, True)
    assert _marker(conn) is not None


def test_when_musicbrainz_credits_someone_else_the_tags_stand(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1: 28 of 219 cached credits name someone else. The stranger gets no artist."""
    client = _Client(
        recordings={RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(THREE, "Stranger")]}}
    )
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)

    _run(conn, monkeypatch, client)

    assert _columns(conn, file_id) == (
        [ONE, TWO],
        [{"name": "A", "mbid": ONE, "join": None}, {"name": None, "mbid": TWO, "join": None}],
    )
    catalog = PgArtistRepository(conn)
    assert catalog.get_by_mbid(THREE) is None
    stub = catalog.get_by_mbid(TWO)
    assert stub is not None
    assert stub.name == TWO


def test_a_row_a_scan_wrote_with_no_names_is_named(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file scanned since 0048 and before this run holds the columns, with null names."""
    client = _Client()
    _cached_artist(conn, ONE, "A")
    _cached_artist(conn, TWO, "B")
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)
    conn.execute(
        "UPDATE library_files SET artist_mbids = %s, credit = %s WHERE id = %s",
        (
            [ONE, TWO],
            Jsonb(
                [
                    {"name": None, "mbid": ONE, "join": None},
                    {"name": None, "mbid": TWO, "join": None},
                ]
            ),
            file_id,
        ),
    )

    _run(conn, monkeypatch, client)

    credit = _columns(conn, file_id)[1]
    assert credit is not None
    assert [entry["name"] for entry in credit] == ["A", "B"]
    one = PgArtistRepository(conn).get_by_mbid(ONE)
    assert one is not None
    assert one.name == "A"


def test_a_filled_row_is_left_as_it_is(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    named = [{"name": "Kept", "mbid": ONE, "join": ""}]
    file_id = _row(conn, _vorbis("A", mbids=[TWO]))
    conn.execute(
        "UPDATE library_files SET artist_mbids = %s, credit = %s WHERE id = %s",
        ([ONE], Jsonb(named), file_id),
    )

    _run(conn, monkeypatch)

    assert _columns(conn, file_id) == ([ONE], named)


# --- S14's files half and S4's artists ------------------------------------------------------------


def test_several_mbids_each_get_an_artist(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """A named MBID goes through the MusicBrainz upsert, and an unnamed one is stubbed for
    enrichment. A placeholder stays in the file's columns with no artist."""
    _row(conn, _vorbis("A & B & U", ["A", "B", "[unknown]"], [ONE, TWO, UNKNOWN]))
    _row(conn, _vorbis("Nobody", mbids=[THREE, TWO]))

    result = _run(conn, monkeypatch)

    catalog = PgArtistRepository(conn)
    one = catalog.get_by_mbid(ONE)
    assert one is not None
    assert (one.name, one.normalized_name, one.origin.value) == ("A", "a", "musicbrainz")
    two = catalog.get_by_mbid(TWO)
    assert two is not None
    assert two.name == "B"
    stub = catalog.get_by_mbid(THREE)
    assert stub is not None
    assert (stub.name, stub.needs_enhancement) == (THREE, True)
    assert catalog.get_by_mbid(UNKNOWN) is None
    assert result["stubbed"] == 1


def test_a_single_mbid_file_gets_no_artist(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """D6: the linker and enrichment promote those."""
    _row(conn, _vorbis("A", mbids=[ONE]))

    _run(conn, monkeypatch)

    assert PgArtistRepository(conn).get_by_mbid(ONE) is None


def test_the_sweep_runs_after_the_fill(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared-name half reads the scalar, so it reaches this name whether or not the columns
    are filled. The order is what the event shows, and the files half is what needs it."""
    file_id = _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))
    conn.execute("UPDATE library_files SET artist_mbid = %s WHERE id = %s", (ONE, file_id))

    with capture_logs() as events:
        _run(conn, monkeypatch)

    seen = [event["event"] for event in events]
    assert seen.index("artist_credits_filled") < seen.index("artist_identity_sweep_complete")
    assert PgArtistRepository(conn).get_by_mbid(TWO) is not None


# --- S13's fill ---------------------------------------------------------------------------------


def _named_stub(conn: Conn, mbid: str, name: str) -> str:
    """A stub 1a named, left with no normalized name because another artist held it (1a E1)."""
    stub = PgArtistRepository(conn).create_stub(mbid)
    conn.execute("UPDATE artists SET name = %s WHERE id = %s", (name, stub.id))
    return stub.id


def _link(conn: Conn, log_name: str, artist_id: str) -> str:
    name = conn.execute(
        """INSERT INTO broadcast_artists (id, original_name, normalized_name)
           VALUES (gen_random_uuid(), %s, %s) RETURNING id""",
        (log_name, log_name.lower()),
    ).fetchone()
    assert name is not None
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, state, origin, strength, evidence)
           VALUES (%s, %s, 'active', 'manual', 100, '{"version": 0}')""",
        (name["id"], artist_id),
    )
    return str(name["id"])


def _artist_row(conn: Conn, artist_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM artists WHERE id = %s", (artist_id,)).fetchone()
    assert row is not None
    return dict(row)


def _suggestions(conn: Conn) -> list[tuple[str, str, str, list[str]]]:
    rows = conn.execute(
        """SELECT normalized_name, reason, state, candidates FROM library_suggestions
            ORDER BY normalized_name, reason"""
    ).fetchall()
    return [(r["normalized_name"], r["reason"], r["state"], r["candidates"]) for r in rows]


def test_null_normalized_stubs_filled(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """By S13's rule: a name-only holder stays as it is, with one ``different_acts`` row. Two
    stubs named alike with no holder get no row."""
    holder = PgArtistRepository(conn).upsert_local_artist("Metallica", "metallica")
    held = _named_stub(conn, ONE, "Metallica")
    twin_one = _named_stub(conn, TWO, "Nirvana")
    twin_two = _named_stub(conn, THREE, "Nirvana")
    before = _artist_row(conn, holder)

    _run(conn, monkeypatch)

    assert _artist_row(conn, held)["normalized_name"] == "metallica"
    assert _artist_row(conn, twin_one)["normalized_name"] == "nirvana"
    assert _artist_row(conn, twin_two)["normalized_name"] == "nirvana"
    holder_after = _artist_row(conn, holder)
    assert {k: holder_after[k] for k in ("mbid", "name", "origin", "normalized_name")} == {
        k: before[k] for k in ("mbid", "name", "origin", "normalized_name")
    }
    assert _suggestions(conn) == [("metallica", "different_acts", "open", [ONE])]


def test_a_symbol_only_stub_keeps_null(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _named_stub(conn, ONE, "!!!")

    _run(conn, monkeypatch)

    assert _artist_row(conn, stub)["normalized_name"] is None
    assert _suggestions(conn) == []


def test_an_unnamed_stub_is_left_for_enrichment(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = PgArtistRepository(conn).create_stub(ONE)

    _run(conn, monkeypatch)

    row = _artist_row(conn, stub.id)
    assert (row["name"], row["normalized_name"]) == (ONE, None)


def test_a_filled_stub_writes_no_name_and_moves_no_link(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = PgArtistRepository(conn).upsert_local_artist("Metallica", "metallica")
    stub = _named_stub(conn, ONE, "Metallica")
    links = [_link(conn, "METALLICA", holder), _link(conn, "METALLICA LIVE", stub)]
    before = _artist_row(conn, stub)
    query = "SELECT id, artist_id, state, updated_at FROM log_name_links ORDER BY id"
    links_before = conn.execute(query).fetchall()

    _run(conn, monkeypatch)

    after = _artist_row(conn, stub)
    assert after["name"] == before["name"]
    assert after["needs_enhancement"] == before["needs_enhancement"]
    assert conn.execute(query).fetchall() == links_before
    assert len(links) == 2


def test_two_stubs_on_one_holder_share_its_row(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    PgArtistRepository(conn).upsert_local_artist("Metallica", "metallica")
    _named_stub(conn, ONE, "Metallica")
    _named_stub(conn, TWO, "Metallica")

    _run(conn, monkeypatch)

    assert _suggestions(conn) == [("metallica", "different_acts", "open", sorted([ONE, TWO]))]


# --- S10's fill ---------------------------------------------------------------------------------


def _names(conn: Conn, artist_id: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, source FROM artist_names WHERE artist_id = %s", (artist_id,)
    ).fetchall()
    return {(r["name"], r["source"]) for r in rows}


def _insert_artist(conn: Conn, name: str, normalized: str, mbid: str | None) -> str:
    """As the catalog holds one from before 0048, with no ``artist_names`` row."""
    row = conn.execute(
        """INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,
                                needs_enhancement)
           VALUES (gen_random_uuid()::text, %s, %s, %s, %s, %s, FALSE)
           RETURNING id""",
        (name, name, normalized, mbid, "musicbrainz" if mbid else "local"),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def test_existing_artists_get_their_names(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    mb = _insert_artist(conn, "Alice Cooper", "alice cooper", ONE)
    local = _insert_artist(conn, "Local Band", "local band", None)

    _run(conn, monkeypatch)

    assert _names(conn, mb) == {("Alice Cooper", "musicbrainz_name")}
    assert _names(conn, local) == {("Local Band", "tag")}


def test_an_unnamed_stub_gets_no_name(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = PgArtistRepository(conn).create_stub(ONE)

    _run(conn, monkeypatch)

    assert _names(conn, stub.id) == set()


def test_cached_aliases_name_candidate_artists(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """C46: what the cache has. ``artist-search`` hits are kept whole, and ``artist:`` rows from
    before #231 still hold aliases. An artist the catalog doesn't hold gets none, and 2a fetches
    no alias."""
    artist = _insert_artist(conn, "Alice Cooper", "alice cooper", ONE)
    _cache(
        conn,
        "artist-search:alice cooper",
        {
            "artists": [
                {"id": ONE, "name": "Alice Cooper", "aliases": [{"name": "The Coop"}]},
                {"id": TWO, "name": "Stranger", "aliases": [{"name": "Not In The Catalog"}]},
            ]
        },
    )
    _cache(
        conn,
        f"artist:{ONE}",
        {"id": ONE, "name": "Alice Cooper", "aliases": [{"name": "Vincent Furnier"}]},
        expired=True,
    )
    client = _Client()

    _run(conn, monkeypatch, client)

    assert _names(conn, artist) == {
        ("Alice Cooper", "musicbrainz_name"),
        ("The Coop", "musicbrainz_alias"),
        ("Vincent Furnier", "musicbrainz_alias"),
    }
    assert client.calls == []
    rows = conn.execute("SELECT name FROM artist_names").fetchall()
    assert "Not In The Catalog" not in {r["name"] for r in rows}


def test_a_cached_row_with_no_aliases_adds_none(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    artist = _insert_artist(conn, "Alice Cooper", "alice cooper", ONE)
    _cached_artist(conn, ONE, "Alice Cooper")
    _cache(conn, "artist-search:nobody", {"artists": []})
    _cache(conn, "artist-search:malformed", {"artists": [{"name": "No Id"}, {"id": ONE}]})

    _run(conn, monkeypatch)

    assert _names(conn, artist) == {("Alice Cooper", "musicbrainz_name")}


def test_paired_tag_spellings_name_their_artist(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    artist = _insert_artist(conn, "Alice Cooper", "alice cooper", ONE)
    _row(conn, _vorbis("Alice Cooper Band & B", ["Alice Cooper Band", "B"], [ONE, TWO]))
    _row(conn, _vorbis("ALICE COOPER", mbids=[ONE]))

    _run(conn, monkeypatch)

    assert _names(conn, artist) == {
        ("Alice Cooper", "musicbrainz_name"),
        ("Alice Cooper Band", "tag"),
        ("ALICE COOPER", "tag"),
    }


def test_no_log_spelling_reaches_the_names(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """C43: a log name linked to the artist adds nothing."""
    artist = _insert_artist(conn, "Alice Cooper", "alice cooper", ONE)
    name = conn.execute(
        """INSERT INTO broadcast_artists (id, original_name, normalized_name)
           VALUES (gen_random_uuid(), 'ALICE COOPER LIVE', 'alice cooper live') RETURNING id"""
    ).fetchone()
    assert name is not None
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, state, origin, strength, evidence)
           VALUES (%s, %s, 'active', 'manual', 100, '{"version": 0}')""",
        (name["id"], artist),
    )

    _run(conn, monkeypatch)

    assert _names(conn, artist) == {("Alice Cooper", "musicbrainz_name")}


# --- E3: MusicBrainz down ---------------------------------------------------------------------


def test_outage(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """AUD-R028: unavailable is no evidence. The file waits with its columns unfilled, so no stub
    is made for it, and the marker waits. The next run looks it up, and the marker is set."""
    down = _Client(error_mbids={RECORDING})
    waiting = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)
    paired = _row(conn, _vorbis("C & D", ["C", "D"], [THREE, TWO]))

    first = _run(conn, monkeypatch, down)

    assert _columns(conn, waiting) == (None, None)
    assert _columns(conn, paired)[0] == [THREE, TWO]
    catalog = PgArtistRepository(conn)
    assert catalog.get_by_mbid(ONE) is None
    assert catalog.get_by_mbid(THREE) is None  # the whole sweep waits, or the stubs wouldn't
    assert (first["waiting"], first["stubbed"], first["done"]) == (1, 0, False)
    assert _marker(conn) is None

    up = _Client(recordings={RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]}})
    second = _run(conn, monkeypatch, up)

    credit = _columns(conn, waiting)[1]
    assert credit is not None
    assert [entry["name"] for entry in credit] == ["A", "B"]
    assert (second["files"], second["waiting"], second["done"]) == (1, 0, True)
    assert _marker(conn) is not None
    one = catalog.get_by_mbid(ONE)
    assert one is not None
    assert one.name == "A"
    three = catalog.get_by_mbid(THREE)
    assert three is not None
    assert three.name == "C"


def test_an_outage_stubs_nothing_a_scan_left_unnamed(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file scanned since 0048 holds the columns, with null names. E3 still holds: the stubs
    wait, and the next run names it through S6."""
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)
    unnamed = [
        {"name": None, "mbid": ONE, "join": None},
        {"name": None, "mbid": TWO, "join": None},
    ]
    conn.execute(
        "UPDATE library_files SET artist_mbids = %s, credit = %s WHERE id = %s",
        ([ONE, TWO], Jsonb(unnamed), file_id),
    )

    first = _run(conn, monkeypatch, _Client(error_mbids={RECORDING}))

    catalog = PgArtistRepository(conn)
    assert (first["waiting"], first["done"]) == (1, False)
    assert catalog.get_by_mbid(ONE) is None
    assert _columns(conn, file_id)[1] == unnamed

    up = _Client(recordings={RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]}})
    _run(conn, monkeypatch, up)

    one = catalog.get_by_mbid(ONE)
    assert one is not None
    assert one.name == "A"


def test_a_partial_outage_keeps_what_it_got(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lookup_recordings`` omits a failed key: only that recording's files wait."""
    client = _Client(
        recordings={OTHER_RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]}},
        error_mbids={RECORDING},
    )
    waiting = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=RECORDING)
    named = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording=OTHER_RECORDING)

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, waiting) == (None, None)
    assert _columns(conn, named)[0] == [ONE, TWO]
    assert (result["waiting"], result["done"]) == (1, False)


# --- E6: a re-tag between scan and backfill -------------------------------------------------------


def _flac(path: Path, levels: int = 0, **tags: list[str]) -> Path:
    from mutagen.flac import FLAC

    path.parent.mkdir(parents=True, exist_ok=True)
    write_flac(path, [(levels, levels)])
    audio = FLAC(str(path))
    for key, values in tags.items():
        audio[key] = values
    audio.save()
    return path


def test_retag(conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-tag changes the stat, so the next scan rewrites both columns from the new tags, over
    what the backfill wrote."""
    root = tmp_path / "library"
    path = _flac(root / "a.flac", artist=["A & B"], musicbrainz_artistid=[ONE, TWO])
    full_scan(conn, root)
    conn.execute("UPDATE library_files SET artist_mbids = NULL, credit = NULL")
    _cached_artist(conn, ONE, "A")
    _cached_artist(conn, TWO, "B")
    _run(conn, monkeypatch)
    row = conn.execute("SELECT id FROM library_files").fetchone()
    assert row is not None
    credit = _columns(conn, str(row["id"]))[1]
    assert credit is not None
    assert credit[0]["name"] == "A"

    _flac(path, 1, artist=["C"], musicbrainz_artistid=[THREE])
    full_scan(conn, root)

    assert _columns(conn, str(row["id"])) == (
        [THREE],
        [{"name": "C", "mbid": THREE, "join": ""}],
    )


def test_an_mp3_that_returns_is_filled_by_the_scan(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E6's other half: the backfill skipped it, and its columns are NULL, so a scan fills them."""
    root = tmp_path / "library"
    root.mkdir()
    path = root / "a.mp3"
    file_id = _row(conn, _lost_separators(ONE, TWO), path=str(path), fmt="mp3")
    _run(conn, monkeypatch)
    assert _columns(conn, file_id) == (None, None)

    _mp3(path, "A & B", [ONE, TWO], ["A", "B"])
    full_scan(conn, root)

    assert _columns(conn, file_id)[0] == [ONE, TWO]


# --- the marker and the progress ------------------------------------------------------------------


def test_a_finished_backfill_does_nothing(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    _row(conn, _vorbis("A", mbids=[ONE]))
    assert _run(conn, monkeypatch)["done"] is True
    marker = _marker(conn)
    assert marker is not None

    late = _row(conn, _vorbis("B", mbids=[TWO]))
    client = _Client()
    again = _run(conn, monkeypatch, client)

    assert _columns(conn, late) == (None, None)
    assert again["files"] == 0
    assert again["done"] is True
    assert client.calls == []
    assert _marker(conn) == marker


def test_the_run_reports_on_its_own_progress_type(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))

    _run(conn, monkeypatch)

    row = conn.execute(
        """SELECT status, progress_data FROM progress_tracking
            WHERE task_type = 'artist_credits_backfill'"""
    ).fetchone()
    assert row is not None
    assert row["status"] == "completed"
    assert row["progress_data"]["files"] == 1
    assert row["progress_data"]["paired"] == 1


def test_the_run_counts_for_the_pr(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client(
        recordings={RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]}}
    )
    _row(conn, _vorbis("X", mbids=[THREE]))
    _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))
    _cached_artist(conn, THREE, "Three")
    _cached_artist(conn, TWO, "Two")
    _row(conn, _vorbis("Y", mbids=[THREE, TWO]))
    _row(conn, _vorbis("Z", mbids=[ONE, TWO]), recording=RECORDING)
    _row(conn, _vorbis("Q", mbids=[THREE, "0a000000-0000-4000-8000-000000000009"]))

    result = _run(conn, monkeypatch, client)

    assert result == {
        "files": 5,
        "paired": 1,
        "named_from_cache": 1,
        "looked_up": 1,
        "stubbed": 1,
        "absent": 0,
        "waiting": 0,
        "done": True,
    }


def test_a_large_library_is_read_in_batches(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artist_credit_backfill_tasks, "BATCH_SIZE", 2)
    ids = [_row(conn, _vorbis("A", mbids=[ONE])) for _ in range(5)]

    result = _run(conn, monkeypatch)

    assert result["files"] == 5
    assert all(_columns(conn, file_id)[0] == [ONE] for file_id in ids)


def test_a_failed_sweep_read_holds_the_marker(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """The columns are filled and no artist was made, so the next run has work to do."""

    def refused(_self: object) -> list[object]:
        raise psycopg.OperationalError("the read was refused")

    monkeypatch.setattr(PgArtistTagFilesRepository, "list_tag_groups", refused)
    _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))

    result = _run(conn, monkeypatch)

    assert result["done"] is False
    assert _marker(conn) is None


# --- what the review found ---------------------------------------------------------------------


def test_a_malformed_recording_mbid_is_never_looked_up(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MusicBrainz answers 400, which ``lookup_recordings`` omits as it does an outage, so the file
    would wait and the marker would never be set. Enrichment refuses these without a lookup too."""
    client = _Client()
    file_id = _row(conn, _vorbis("A & B", mbids=[ONE, TWO]), recording="not-an-mbid")

    result = _run(conn, monkeypatch, client)

    assert client.calls == []
    assert _columns(conn, file_id)[0] == [ONE, TWO]
    assert (result["waiting"], result["stubbed"], result["done"]) == (0, 2, True)


def test_a_paired_file_takes_a_join_from_a_cached_credit(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C69: a credited-as name isn't in the print, so its join comes from the MusicBrainz credit.
    Only what the cache holds: a file that pairs asks nobody."""
    client = _Client()
    _cache(
        conn,
        f"recording:{RECORDING}",
        {"artist-credit": [_credit(ONE, "Strait Up", " & "), _credit(TWO, "Lajon", "")]},
    )
    file_id = _row(
        conn, _vorbis("Snot & Lajon", ["Strait Up", "Lajon"], [ONE, TWO]), recording=RECORDING
    )

    result = _run(conn, monkeypatch, client)

    assert _columns(conn, file_id)[1] == [
        {"name": "Strait Up", "mbid": ONE, "join": " & "},
        {"name": "Lajon", "mbid": TWO, "join": ""},
    ]
    assert client.calls == []
    assert result["paired"] == 1


def test_a_full_lookup_gives_its_joining_words(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """``lookup_recording`` returns full rows; only a slim search row has no join phrase."""
    client = _Client(
        recordings={RECORDING: {"artist-credit": [_credit(ONE, "A", " + "), _credit(TWO, "B", "")]}}
    )
    file_id = _row(conn, _vorbis("Printed Otherwise", mbids=[ONE, TWO]), recording=RECORDING)

    _run(conn, monkeypatch, client)

    assert _columns(conn, file_id)[1] == [
        {"name": "A", "mbid": ONE, "join": " + "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]


def test_a_saved_name_with_a_comma_stays_one_name(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = _row(
        conn,
        _vorbis("Earth, Wind & Fire & B", ["Earth, Wind & Fire", "B"], [ONE, TWO]),
    )

    _run(conn, monkeypatch)

    assert _columns(conn, file_id)[1] == [
        {"name": "Earth, Wind & Fire", "mbid": ONE, "join": " & "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]


def test_a_nul_in_a_saved_name_doesnt_stop_the_run(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSONB refuses one, and the backfill would die on every run."""
    file_id = _row(
        conn,
        {
            "artist": "['AB & C']",
            "artists": str(["A\x00B", "C"]),
            "musicbrainz_artistid": str([ONE, TWO]),
        },
    )

    result = _run(conn, monkeypatch)

    assert result["done"] is True
    credit = _columns(conn, file_id)[1]
    assert credit is not None
    assert credit[0]["name"] == "AB"


def test_one_mbid_beside_a_placeholder_asks_nobody(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It isn't several (D6), so its names aren't looked up and it makes no artist."""
    client = _Client()
    file_id = _row(conn, _vorbis("A & U", mbids=[ONE, UNKNOWN]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert client.calls == []
    assert _columns(conn, file_id)[0] == [ONE, UNKNOWN]
    assert PgArtistRepository(conn).get_by_mbid(ONE) is None
    assert result["stubbed"] == 0


def test_an_unnamed_placeholder_asks_nobody_once_the_rest_are_named(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client()
    _cached_artist(conn, ONE, "A")
    _cached_artist(conn, TWO, "B")
    _row(conn, _vorbis("Other", mbids=[ONE, TWO, UNKNOWN]), recording=RECORDING)

    result = _run(conn, monkeypatch, client)

    assert client.calls == []
    assert result["named_from_cache"] == 1


def test_a_swept_artist_keeps_each_spelling_as_a_tag(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artist's own name came from tags here, so it isn't recorded as MusicBrainz's. The
    spelling fewer files carry is a name too, and a cached alias reaches an artist the sweep
    made."""
    _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))
    _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))
    _row(conn, _vorbis("Ayy & B", ["Ayy", "B"], [ONE, TWO]))
    _cache(conn, f"artist:{ONE}", {"id": ONE, "name": "A", "aliases": [{"name": "Alias"}]})

    _run(conn, monkeypatch)

    artist = PgArtistRepository(conn).get_by_mbid(ONE)
    assert artist is not None
    assert artist.name == "A"
    assert _names(conn, artist.id) == {
        ("A", "tag"),
        ("Ayy", "tag"),
        ("Alias", "musicbrainz_alias"),
    }
