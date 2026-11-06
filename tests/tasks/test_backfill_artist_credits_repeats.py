"""What the build's fresh review found in the backfill (phase 2a S3, S4, S10, S14), on PostgreSQL.

A sweep can reach an MBID before the backfill's names do (a run cut short, or a scan's sweep), and
its name came from a tag, so a later run mustn't record it as MusicBrainz's. A stub ``list_all``
hides still gets its spellings. The run shows while it works, not only when it ends. An MP3 the
scan hasn't re-read yet can still be looked up by the recording MBID its file holds.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from mutagen.id3 import ID3, TIT2, TPE1, TXXX, UFID
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.tasks import artist_credit_backfill_tasks, huey_app
from backend.tasks.artist_credit_backfill_tasks import backfill_artist_credits_task
from backend.tasks.artist_identity_tasks import artist_identity_sweep_task
from tests.fakes.mb_client import FakeMbClient
from tests.fixtures.audio_builders import write_mp3

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
RECORDING = "0b000000-0000-4000-8000-000000000001"
OTHER_RECORDING = "0b000000-0000-4000-8000-000000000002"


class _Client(FakeMbClient):
    """A fake client the task can open as it opens the real one."""

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection
    get_settings.cache_clear()


def _run(
    conn: Conn, monkeypatch: pytest.MonkeyPatch, client: _Client | None = None
) -> dict[str, Any]:
    conn.commit()
    used = client if client is not None else _Client()
    monkeypatch.setattr(
        artist_credit_backfill_tasks, "MusicBrainzApiClient", lambda *_a, **_k: used
    )
    result: dict[str, Any] = backfill_artist_credits_task.call_local()
    conn.rollback()
    return result


def _vorbis(artist: str, names: list[str] | None, mbids: list[str]) -> dict[str, str]:
    raw = {"artist": str([artist]), "musicbrainz_artistid": str(mbids)}
    if names is not None:
        raw["artists"] = str(names)
    return raw


def _row(
    conn: Conn,
    raw: object,
    *,
    path: str | None = None,
    fmt: str = "flac",
    recording: str | None = None,
) -> str:
    row = conn.execute(
        """INSERT INTO library_files
               (file_path, format, raw_metadata, file_status, recording_mbid,
                artist_name, normalized_artist_name, track_title)
           VALUES (%s, %s, %s, 'present', %s, 'A & B', 'a b', 'Some Song')
           RETURNING id""",
        (path or f"/music/{uuid4().hex}.{fmt}", fmt, Jsonb(raw), recording),
    ).fetchone()
    assert row is not None
    return str(row["id"])


def _fill_from_a_scan(conn: Conn, file_id: str, mbids: list[str], names: list[str]) -> None:
    """What a scan since 0048 writes for a file whose tags pair."""
    credit = [{"name": n, "mbid": m, "join": ""} for m, n in zip(mbids, names, strict=True)]
    conn.execute(
        "UPDATE library_files SET artist_mbids = %s, credit = %s WHERE id = %s",
        (mbids, Jsonb(credit), file_id),
    )


def _names(conn: Conn, artist_id: str) -> set[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, source FROM artist_names WHERE artist_id = %s", (artist_id,)
    ).fetchall()
    return {(row["name"], row["source"]) for row in rows}


def _artist_id(conn: Conn, mbid: str) -> str:
    artist = PgArtistRepository(conn).get_by_mbid(mbid)
    assert artist is not None
    return artist.id


def _columns(conn: Conn, file_id: str) -> tuple[list[str] | None, list[dict[str, Any]] | None]:
    row = conn.execute(
        "SELECT artist_mbids, credit FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return row["artist_mbids"], row["credit"]


def _credit(mbid: str, name: str) -> dict[str, Any]:
    return {"name": name, "artist": {"id": mbid, "name": name}, "joinphrase": ""}


# --- a tag's name stays a tag's ---------------------------------------------------------------


def test_a_scans_sweep_before_the_backfill_keeps_its_name_a_tag(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_id = _row(conn, _vorbis("Ayy & B", ["Ayy", "B"], [ONE, TWO]))
    _fill_from_a_scan(conn, file_id, [ONE, TWO], ["Ayy", "B"])
    conn.commit()
    artist_identity_sweep_task.call_local()
    conn.rollback()

    _run(conn, monkeypatch)

    assert _names(conn, _artist_id(conn, ONE)) == {("Ayy", "tag")}


def test_a_run_cut_short_keeps_the_sweeps_name_a_tag(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    _row(conn, _vorbis("Ayy & B", ["Ayy", "B"], [ONE, TWO]))
    real = artist_credit_backfill_tasks._fill_other_names

    def lost_connection(_repos: object) -> None:
        raise psycopg.OperationalError("the connection went away")

    monkeypatch.setattr(artist_credit_backfill_tasks, "_fill_other_names", lost_connection)
    with pytest.raises(psycopg.OperationalError):
        _run(conn, monkeypatch)
    conn.rollback()

    monkeypatch.setattr(artist_credit_backfill_tasks, "_fill_other_names", real)
    result = _run(conn, monkeypatch)

    assert result["done"] is True
    assert _names(conn, _artist_id(conn, ONE)) == {("Ayy", "tag")}


def test_an_artist_of_musicbrainz_with_a_new_name_still_gets_it(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipping what is recorded asks about this name, not whether the artist has any row."""
    catalog = PgArtistRepository(conn)
    artist = catalog.upsert_musicbrainz_artist(ONE, "Old", "Old", "old")
    conn.execute("UPDATE artists SET name = 'New' WHERE id = %s", (artist,))

    _run(conn, monkeypatch)

    assert _names(conn, artist) == {("Old", "musicbrainz_name"), ("New", "musicbrainz_name")}


# --- a stub's spellings -----------------------------------------------------------------------


def test_a_symbol_only_spelling_reaches_its_stub(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S3 reads spellings once, so one that misses its artist now is gone for good."""
    _row(conn, _vorbis("!!! & B", ["!!!", "B"], [ONE, TWO]))

    _run(conn, monkeypatch)

    stub = _artist_id(conn, ONE)
    assert _names(conn, stub) == {("!!!", "tag")}


def test_a_stub_from_before_the_backfill_gets_its_spelling(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = PgArtistRepository(conn).create_stub(ONE)
    _row(conn, _vorbis("Foo & B", ["Foo", "B"], [ONE, TWO]))

    _run(conn, monkeypatch)

    assert _names(conn, stub.id) == {("Foo", "tag")}
    artist = PgArtistRepository(conn).get_by_mbid(ONE)
    assert artist is not None
    assert artist.name == ONE  # the spelling doesn't name it: enrichment does


def test_a_cached_alias_reaches_a_stub(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = PgArtistRepository(conn).create_stub(ONE)
    conn.execute(
        """INSERT INTO mb_cache (id, cache_key, entity_type, entity_mbid, response_data, expires_at)
           VALUES (gen_random_uuid(), %s, 'artist', %s, %s, now() + interval '1 day')""",
        (
            f"artist:{ONE}",
            ONE,
            Jsonb({"id": ONE, "name": "Real Name", "aliases": [{"name": "An Alias"}]}),
        ),
    )

    _run(conn, monkeypatch)

    assert _names(conn, stub.id) == {("An Alias", "musicbrainz_alias")}


# --- the sweep asks once -----------------------------------------------------------------------


def test_a_repeat_sweep_asks_nothing_per_mbid(conn: Conn, monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs after every watcher batch with changes, and most MBIDs have an artist by then."""
    file_id = _row(conn, _vorbis("A & B", ["A", "B"], [ONE, TWO]))
    _fill_from_a_scan(conn, file_id, [ONE, TWO], ["A", "B"])
    conn.commit()
    artist_identity_sweep_task.call_local()
    conn.rollback()

    def per_mbid(_self: object, _mbid: str) -> None:
        raise AssertionError("the sweep asked about one MBID")

    monkeypatch.setattr(PgArtistRepository, "get_by_mbid", per_mbid)

    assert artist_identity_sweep_task.call_local()["artists_made"] == 0


# --- the run shows while it works --------------------------------------------------------------


def _running_rows(database_url: str) -> list[dict[str, Any]]:
    with psycopg.connect(database_url, row_factory=dict_row) as reader:
        return reader.execute(
            """SELECT status, progress_data FROM progress_tracking
                WHERE task_type = 'artist_credits_backfill'"""
        ).fetchall()


class _WatchingClient(_Client):
    """Reads the run's progress row as each lookup starts, from a second connection."""

    def __init__(self, database_url: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.database_url = database_url
        self.seen: list[tuple[str, int]] = []

    def lookup_recordings(self, mbids: list[str]) -> dict[str, Any]:
        [row] = _running_rows(self.database_url)
        self.seen.append((row["status"], row["progress_data"]["processed"]))
        return dict(super().lookup_recordings(mbids))


def test_the_run_shows_running_and_moves_with_each_batch(
    conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(artist_credit_backfill_tasks, "BATCH_SIZE", 1)
    _row(conn, _vorbis("A & B", None, [ONE, TWO]), recording=RECORDING)
    _row(conn, _vorbis("A & B", None, [ONE, TWO]), recording=OTHER_RECORDING)
    client = _WatchingClient(
        migrated_db,
        recordings={
            RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]},
            OTHER_RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]},
        },
    )

    result = _run(conn, monkeypatch, client)

    assert client.seen == [("running", 0), ("running", 1)]
    assert result["done"] is True
    [row] = _running_rows(migrated_db)
    assert row["status"] == "completed"


# --- what the rows hold ------------------------------------------------------------------------


def test_a_row_holding_its_json_as_a_string_is_read(
    conn: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As ``PgLibraryFileRepository._row_to_model`` reads one: it would otherwise kill every run."""
    file_id = _row(conn, json.dumps(_vorbis("A & B", ["A", "B"], [ONE, TWO])))

    result = _run(conn, monkeypatch)

    assert _columns(conn, file_id)[0] == [ONE, TWO]
    assert result["done"] is True


def test_an_mp3_is_looked_up_by_the_recording_its_file_holds(
    conn: Conn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2's UFID reaches the column at the next full scan, but the file can be named now. Without
    this, the MBIDs become stubs beside what their name-only artists may hold (D10)."""
    path = tmp_path / "a.mp3"
    write_mp3(path)
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Song"]))
    tags.add(UFID(owner="http://musicbrainz.org", data=RECORDING.encode("ascii")))
    tags.add(TPE1(encoding=3, text=["A & B"]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=[ONE, TWO]))
    tags.save(str(path))
    file_id = _row(conn, {"TPE1": "A & B"}, path=str(path), fmt="mp3")
    client = _Client(
        recordings={RECORDING: {"artist-credit": [_credit(ONE, "A"), _credit(TWO, "B")]}}
    )

    result = _run(conn, monkeypatch, client)

    credit = _columns(conn, file_id)[1]
    assert credit is not None
    assert [entry["name"] for entry in credit] == ["A", "B"]
    assert result["looked_up"] == 1
    row = conn.execute(
        "SELECT recording_mbid FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    assert row["recording_mbid"] is None  # the re-read still writes only the two columns
