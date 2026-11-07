"""The scan writes ``artist_mbids`` and ``credit`` (phase 2a S1, S14's files half, E6; C26, C69,
D6), on PostgreSQL with real files.

Only when the file's stat changed, or the columns are NULL: a full scan reads and upserts every
file, and must never replace names the backfill looked up. A row upserted from its stored model
leaves them alone. The name columns and the scalar stay as the tags give them (C26).

S14's files half: each real MBID of a file holding several gets an artist, last in the scan. A
named one goes through the MusicBrainz upsert with the spelling most of its files carry, and an
unnamed one becomes a stub that enrichment names. A file holding one MBID makes none (D6).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from mutagen.flac import FLAC
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.tasks import huey_app
from backend.tasks.artist_identity_tasks import sweep_artist_identities
from backend.tasks.library_watcher_tasks import library_scan_files_task
from tests.fixtures.audio_builders import write_flac
from tests.integration.missing_file_seed import full_scan

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

_MIGRATIONS = Path(__file__).resolve().parents[2] / "backend" / "db" / "migrations"

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
THREE = "0a000000-0000-4000-8000-000000000003"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
LOOKED_UP = [
    {"name": "Named By A Lookup", "mbid": ONE, "join": None},
    {"name": "Named Too", "mbid": TWO, "join": None},
]


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def task_database(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The settings name the test database, so a task's own connections use it; whatever it
    hands off stays off the queue."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _flac(path: Path, levels: int = 0, **tags: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_flac(path, [(levels, levels)])
    audio = FLAC(str(path))
    for key, values in tags.items():
        audio[key] = values
    audio.save()
    return path


def _paired(path: Path, levels: int = 0) -> Path:
    return _flac(
        path, levels, artist=["A & B"], artists=["A", "B"], musicbrainz_artistid=[ONE, TWO]
    )


def _unpaired(path: Path, levels: int = 0) -> Path:
    return _flac(path, levels, artist=["A & B"], musicbrainz_artistid=[ONE, TWO])


def _row(conn: Conn, path: Path) -> dict[str, Any]:
    row = conn.execute(
        """SELECT artist_mbids, credit, artist_name, normalized_artist_name, artist_mbid,
                  file_status, indexed_at
             FROM library_files WHERE file_path = %s""",
        (str(path),),
    ).fetchone()
    assert row is not None
    return dict(row)


def _look_up_names(conn: Conn, path: Path) -> None:
    """What the backfill's lookups leave on a row."""
    conn.execute(
        "UPDATE library_files SET credit = %s WHERE file_path = %s",
        (Jsonb(LOOKED_UP), str(path)),
    )
    conn.commit()


def _clear_columns(conn: Conn, path: Path) -> None:
    conn.execute(
        "UPDATE library_files SET artist_mbids = NULL, credit = NULL WHERE file_path = %s",
        (str(path),),
    )
    conn.commit()


def _named_artists(conn: Conn) -> list[tuple[str | None, str, str | None]]:
    rows = conn.execute(
        "SELECT mbid, name, normalized_name FROM artists ORDER BY mbid NULLS FIRST, name"
    ).fetchall()
    return [(r["mbid"], r["name"], r["normalized_name"]) for r in rows]


# --- the schema -----------------------------------------------------------------------------------


def test_the_schema_after_0048(conn: Conn) -> None:
    applied = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version LIKE '0048\\_%'"
    ).fetchone()
    assert applied is not None
    [migration] = _MIGRATIONS.glob("0048_*.sql")
    statements = [
        line
        for line in migration.read_text(encoding="utf-8").upper().splitlines()
        if not line.lstrip().startswith("--")
    ]
    assert statements[0].startswith("SET LOCAL LOCK_TIMEOUT")

    columns = {
        r["column_name"]: (r["udt_name"], r["is_nullable"], r["column_default"])
        for r in conn.execute(
            """SELECT column_name, udt_name, is_nullable, column_default
                 FROM information_schema.columns
                WHERE table_name = 'library_files'
                  AND column_name IN ('artist_mbids', 'credit')"""
        ).fetchall()
    }
    assert columns == {
        "artist_mbids": ("_text", "YES", None),
        "credit": ("jsonb", "YES", None),
    }
    [index] = [
        r["indexdef"]
        for r in conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'library_files'"
            " AND indexdef LIKE '%artist_mbids%'"
        ).fetchall()
    ]
    assert "USING gin" in index


def test_a_row_starts_unfilled(conn: Conn) -> None:
    """NULL is "not read yet", which S3 and a scan fill. An untagged file is ``{}``."""
    conn.execute(
        "INSERT INTO library_files (file_path, format) VALUES (%s, 'flac')",
        (f"/music/{uuid4().hex}.flac",),
    )
    row = conn.execute("SELECT artist_mbids, credit FROM library_files").fetchone()
    assert row == {"artist_mbids": None, "credit": None}


# --- a scan writes --------------------------------------------------------------------------------


def test_a_scan_writes_both_columns(conn: Conn, tmp_path: Path) -> None:
    path = _paired(tmp_path / "library" / "a.flac")

    full_scan(conn, tmp_path / "library")

    row = _row(conn, path)
    assert row["artist_mbids"] == [ONE, TWO]
    assert row["credit"] == [
        {"name": "A", "mbid": ONE, "join": " & "},
        {"name": "B", "mbid": TWO, "join": ""},
    ]


def test_names_the_tags_cant_give_stay_null(conn: Conn, tmp_path: Path) -> None:
    path = _unpaired(tmp_path / "library" / "a.flac")

    full_scan(conn, tmp_path / "library")

    assert _row(conn, path)["credit"] == [
        {"name": None, "mbid": ONE, "join": None},
        {"name": None, "mbid": TWO, "join": None},
    ]


def test_an_untagged_file(conn: Conn, tmp_path: Path) -> None:
    path = _flac(tmp_path / "library" / "a.flac", artist=["A"])

    full_scan(conn, tmp_path / "library")

    row = _row(conn, path)
    assert row["artist_mbids"] == []
    assert row["credit"] is None


def test_the_name_columns_and_the_scalar_are_unchanged(conn: Conn, tmp_path: Path) -> None:
    """C26: the first value of each, whatever the new columns hold."""
    path = _flac(
        tmp_path / "library" / "a.flac",
        artist=["A & B", "Another Print"],
        artists=["A", "B"],
        musicbrainz_artistid=[ONE, TWO],
    )

    full_scan(conn, tmp_path / "library")

    row = _row(conn, path)
    assert row["artist_name"] == "A & B"
    assert row["normalized_artist_name"] == normalize_artist("A & B")
    assert row["artist_mbid"] == ONE


# --- only when the stat changed, or the columns are NULL ------------------------------------------


def test_an_unchanged_stat_keeps_looked_up_names(conn: Conn, tmp_path: Path) -> None:
    path = _unpaired(tmp_path / "library" / "a.flac")
    full_scan(conn, tmp_path / "library")
    _look_up_names(conn, path)

    full_scan(conn, tmp_path / "library")

    assert _row(conn, path)["credit"] == LOOKED_UP


def test_a_changed_stat_rewrites_both_columns(conn: Conn, tmp_path: Path) -> None:
    """E6's re-tag: the next scan rewrites both from the new tags."""
    root = tmp_path / "library"
    path = _unpaired(root / "a.flac")
    full_scan(conn, root)
    _look_up_names(conn, path)

    _flac(path, 1, artist=["C"], musicbrainz_artistid=[THREE])
    full_scan(conn, root)

    row = _row(conn, path)
    assert row["artist_mbids"] == [THREE]
    assert row["credit"] == [{"name": "C", "mbid": THREE, "join": ""}]


def test_a_re_tag_that_drops_the_mbids_clears_them(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    path = _paired(root / "a.flac")
    full_scan(conn, root)

    _flac(path, 1, artist=["A"])
    full_scan(conn, root)

    row = _row(conn, path)
    assert row["artist_mbids"] == []
    assert row["credit"] is None


def test_null_columns_fill_though_the_stat_is_unchanged(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    path = _paired(root / "a.flac")
    full_scan(conn, root)
    _clear_columns(conn, path)

    full_scan(conn, root)

    assert _row(conn, path)["artist_mbids"] == [ONE, TWO]


def test_a_file_back_from_missing_keeps_looked_up_names_in_a_full_scan(
    conn: Conn, tmp_path: Path
) -> None:
    """Its ``indexed_at`` moves (D11), but its stat didn't. Moved out and back: same bytes, and
    the same stat when it is restored."""
    root = tmp_path / "library"
    path = _unpaired(root / "a.flac")
    _flac(root / "stays.flac", 1, artist=["Other"])  # a walk that sees nothing judges nothing
    full_scan(conn, root)
    _look_up_names(conn, path)
    away = tmp_path / "away.flac"
    stat = path.stat()
    path.rename(away)
    full_scan(conn, root)
    assert _row(conn, path)["file_status"] == "missing"
    moved_at = _row(conn, path)["indexed_at"]

    away.rename(path)
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    full_scan(conn, root)

    row = _row(conn, path)
    assert row["file_status"] == "present"
    assert row["indexed_at"] != moved_at
    assert row["credit"] == LOOKED_UP


# --- the watcher --------------------------------------------------------------------------------


@pytest.mark.usefixtures("task_database")
def test_the_watcher_writes_a_new_file(conn: Conn, tmp_path: Path) -> None:
    folder = tmp_path / "library"
    path = _paired(folder / "a.flac")

    library_scan_files_task.call_local([str(folder)], uuid4().hex)

    assert _row(conn, path)["artist_mbids"] == [ONE, TWO]


@pytest.mark.usefixtures("task_database")
def test_the_watcher_rewrites_a_changed_file(conn: Conn, tmp_path: Path) -> None:
    folder = tmp_path / "library"
    path = _unpaired(folder / "a.flac")
    library_scan_files_task.call_local([str(folder)], uuid4().hex)
    conn.rollback()
    _look_up_names(conn, path)

    _flac(path, 1, artist=["C"], musicbrainz_artistid=[THREE])
    library_scan_files_task.call_local([str(folder)], uuid4().hex)

    conn.rollback()
    assert _row(conn, path)["artist_mbids"] == [THREE]


@pytest.mark.usefixtures("task_database")
def test_the_watcher_leaves_an_unchanged_file_alone(conn: Conn, tmp_path: Path) -> None:
    folder = tmp_path / "library"
    path = _unpaired(folder / "a.flac")
    library_scan_files_task.call_local([str(folder)], uuid4().hex)
    conn.rollback()
    _look_up_names(conn, path)

    library_scan_files_task.call_local([str(folder)], uuid4().hex)

    conn.rollback()
    assert _row(conn, path)["credit"] == LOOKED_UP


@pytest.mark.usefixtures("task_database")
def test_a_file_the_watcher_restores_keeps_looked_up_names(conn: Conn, tmp_path: Path) -> None:
    """The upsert comes from its stored row, which doesn't carry the columns (S1)."""
    folder = tmp_path / "library"
    path = _unpaired(folder / "a.flac")
    library_scan_files_task.call_local([str(folder)], uuid4().hex)
    conn.rollback()
    _look_up_names(conn, path)
    away = tmp_path / "away.flac"
    path.rename(away)
    library_scan_files_task.call_local([str(folder)], uuid4().hex)
    conn.rollback()
    assert _row(conn, path)["file_status"] == "missing"

    away.rename(path)
    library_scan_files_task.call_local([str(folder)], uuid4().hex)

    conn.rollback()
    row = _row(conn, path)
    assert row["file_status"] == "present"
    assert row["credit"] == LOOKED_UP
    assert row["artist_mbids"] == [ONE, TWO]


def test_a_moved_file_keeps_its_columns(conn: Conn, tmp_path: Path) -> None:
    """A move adopts the old row, so what was looked up for it moves with it."""
    root = tmp_path / "library"
    old = _unpaired(root / "a.flac")
    full_scan(conn, root)
    _look_up_names(conn, old)
    new = root / "moved" / "a.flac"
    new.parent.mkdir()
    old.rename(new)

    full_scan(conn, root)

    assert _row(conn, new)["credit"] == LOOKED_UP


def test_a_stored_row_upserted_again_leaves_them_alone(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    path = _unpaired(root / "a.flac")
    full_scan(conn, root)
    _look_up_names(conn, path)
    repos = RepositoryFactory(conn)
    stored = repos.library_files.get_by_path(str(path))
    assert stored is not None

    repos.library_files.upsert(stored)
    conn.commit()

    row = _row(conn, path)
    assert row["credit"] == LOOKED_UP
    assert row["artist_mbids"] == [ONE, TWO]


# --- S14's files half -----------------------------------------------------------------------------


def test_each_mbid_of_a_file_holding_several_gets_an_artist(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    _paired(root / "a.flac")

    full_scan(conn, root)

    assert _named_artists(conn)[-2:] == [(ONE, "A", "a"), (TWO, "B", "b")]
    artist = PgArtistRepository(conn).get_by_mbid(ONE)
    assert artist is not None
    assert artist.needs_enhancement is True


def test_an_unnamed_mbid_becomes_a_stub(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    _unpaired(root / "a.flac")

    full_scan(conn, root)

    catalog = PgArtistRepository(conn)
    for mbid in (ONE, TWO):
        stub = catalog.get_by_mbid(mbid)
        assert stub is not None
        assert (stub.name, stub.normalized_name, stub.needs_enhancement) == (mbid, None, True)


def test_the_spelling_most_files_carry_names_the_artist(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    for number, spelling in enumerate(["A", "A", "Ayy"]):
        _flac(
            root / f"{number}.flac",
            number,
            artist=[f"{spelling} & B"],
            artists=[spelling, "B"],
            musicbrainz_artistid=[ONE, TWO],
        )

    full_scan(conn, root)

    artist = PgArtistRepository(conn).get_by_mbid(ONE)
    assert artist is not None
    assert artist.name == "A"


def test_a_name_beats_no_name_whichever_file_comes_first(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    _unpaired(root / "0.flac", 0)
    _unpaired(root / "1.flac", 1)
    _paired(root / "2.flac", 2)

    full_scan(conn, root)

    artist = PgArtistRepository(conn).get_by_mbid(ONE)
    assert artist is not None
    assert artist.name == "A"


def test_a_name_that_normalizes_to_nothing_is_a_stub(conn: Conn, tmp_path: Path) -> None:
    """S6 asks by normalized name, and a symbol-only name has none, so enrichment names it."""
    root = tmp_path / "library"
    _flac(
        root / "a.flac",
        artist=["!!! & B"],
        artists=["!!!", "B"],
        musicbrainz_artistid=[ONE, TWO],
    )

    full_scan(conn, root)

    stub = PgArtistRepository(conn).get_by_mbid(ONE)
    assert stub is not None
    assert (stub.name, stub.normalized_name) == (ONE, None)


def test_a_known_mbid_is_left_as_it_is(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    held = PgArtistRepository(conn).upsert_musicbrainz_artist(ONE, "Their Name", "Name, Their", "x")
    conn.commit()
    _paired(root / "a.flac")

    full_scan(conn, root)

    artist = PgArtistRepository(conn).get_by_mbid(ONE)
    assert artist is not None
    assert (artist.id, artist.name) == (held, "Their Name")


def test_a_file_holding_one_mbid_makes_no_artist(conn: Conn, tmp_path: Path) -> None:
    """D6: the linker and enrichment promote those, as before."""
    root = tmp_path / "library"
    _flac(root / "a.flac", artist=["A"], musicbrainz_artistid=[ONE])

    full_scan(conn, root)

    assert PgArtistRepository(conn).get_by_mbid(ONE) is None


def test_a_placeholder_stays_in_the_columns_with_no_artist(conn: Conn, tmp_path: Path) -> None:
    """C7: named or not."""
    root = tmp_path / "library"
    path = _flac(
        root / "a.flac",
        artist=["A & B & [unknown]"],
        artists=["A", "B", "[unknown]"],
        musicbrainz_artistid=[ONE, TWO, UNKNOWN],
    )

    full_scan(conn, root)

    assert _row(conn, path)["artist_mbids"] == [ONE, TWO, UNKNOWN]
    assert PgArtistRepository(conn).get_by_mbid(UNKNOWN) is None
    assert PgArtistRepository(conn).get_by_mbid(ONE) is not None


def test_one_mbid_beside_a_placeholder_isnt_several(conn: Conn, tmp_path: Path) -> None:
    """D6: a placeholder is no performer, so this file holds one MBID, as S6's reading counts it.
    The linker and enrichment promote it."""
    root = tmp_path / "library"
    path = _flac(
        root / "a.flac",
        artist=["A & [unknown]"],
        artists=["A", "[unknown]"],
        musicbrainz_artistid=[ONE, UNKNOWN],
    )

    full_scan(conn, root)

    assert _row(conn, path)["artist_mbids"] == [ONE, UNKNOWN]
    assert PgArtistRepository(conn).get_by_mbid(ONE) is None
    assert PgArtistRepository(conn).get_by_mbid(UNKNOWN) is None


def test_a_missing_file_makes_no_artist(conn: Conn, tmp_path: Path) -> None:
    conn.execute(
        """INSERT INTO library_files
               (file_path, format, artist_name, normalized_artist_name, artist_mbids, credit,
                file_status, missing_since)
           VALUES (%s, 'flac', 'A & B', 'a b', %s, %s, 'missing', now())""",
        (f"/music/{uuid4().hex}.flac", [ONE, TWO], Jsonb(LOOKED_UP)),
    )
    conn.commit()

    sweep_artist_identities(conn, RepositoryFactory(conn))

    assert PgArtistRepository(conn).get_by_mbid(ONE) is None


def test_a_named_mbid_promotes_as_s6_reads(conn: Conn, tmp_path: Path) -> None:
    """D2: a known name goes through S6. The name-only artist of the name, whose files hold no
    other MBID, takes the MBID in place."""
    root = tmp_path / "library"
    catalog = PgArtistRepository(conn)
    name_only = catalog.upsert_local_artist("A", "a")
    conn.commit()
    _paired(root / "a.flac")

    full_scan(conn, root)

    artist = catalog.get_by_mbid(ONE)
    assert artist is not None
    assert artist.id == name_only


def test_a_named_mbid_beside_another_mbids_files_inserts(conn: Conn, tmp_path: Path) -> None:
    """The name holds another MBID, so S6 inserts and the name-only artist stays."""
    root = tmp_path / "library"
    catalog = PgArtistRepository(conn)
    name_only = catalog.upsert_local_artist("A", "a")
    conn.commit()
    _flac(root / "single.flac", 1, artist=["A"], musicbrainz_artistid=[THREE])
    _paired(root / "a.flac")

    full_scan(conn, root)

    artist = catalog.get_by_mbid(ONE)
    assert artist is not None
    assert artist.id != name_only
    held = catalog.get_by_id(name_only)
    assert held is not None
    assert held.mbid is None


def test_a_repeat_makes_nothing(conn: Conn, tmp_path: Path) -> None:
    root = tmp_path / "library"
    _paired(root / "a.flac")
    full_scan(conn, root)
    before = _named_artists(conn)

    sweep = sweep_artist_identities(conn, RepositoryFactory(conn))

    assert _named_artists(conn) == before
    assert sweep is not None
    assert sweep.artists_made == 0


def test_the_sweep_counts_what_it_made(conn: Conn, tmp_path: Path) -> None:
    """Each MBID is one artist made, and a stub is also counted as one."""
    conn.execute(
        """INSERT INTO library_files
               (file_path, format, artist_name, normalized_artist_name, artist_mbids, credit,
                file_status)
           VALUES (%s, 'flac', 'A & B', 'a b', %s, %s, 'present')""",
        (
            f"/music/{uuid4().hex}.flac",
            [ONE, TWO],
            Jsonb(
                [
                    {"name": "A", "mbid": ONE, "join": None},
                    {"name": None, "mbid": TWO, "join": None},
                ]
            ),
        ),
    )
    conn.commit()

    sweep = sweep_artist_identities(conn, RepositoryFactory(conn))

    assert sweep is not None
    assert (sweep.artists_made, sweep.stubs_made, sweep.names_failed) == (2, 1, 0)


def test_one_mbid_failing_leaves_the_others(
    conn: Conn, tmp_path: Path, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each MBID is its own transaction, as each name is, and the next sweep retries it."""
    root = tmp_path / "library"
    _unpaired(root / "a.flac")
    write = PgArtistRepository.create_stub

    def failing(self: PgArtistRepository, mbid: str) -> Any:
        if mbid == ONE:
            raise psycopg.errors.DeadlockDetected("the database refused this one")
        return write(self, mbid)

    with monkeypatch.context() as patch:
        patch.setattr(PgArtistRepository, "create_stub", failing)
        full_scan(conn, root)

    with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as reader:
        assert PgArtistRepository(reader).get_by_mbid(ONE) is None
        assert PgArtistRepository(reader).get_by_mbid(TWO) is not None

    sweep = sweep_artist_identities(conn, RepositoryFactory(conn))

    assert sweep is not None
    assert (sweep.artists_made, sweep.names_failed) == (1, 0)
    assert PgArtistRepository(conn).get_by_mbid(ONE) is not None


def test_an_mbids_single_files_dont_name_it(conn: Conn, tmp_path: Path) -> None:
    """Only files holding several name an MBID here. Counting a single-MBID file's print would
    promote the name-only artist from tags alone, which the locked
    ``test_a_single_mbid_makes_no_artist`` forbids and D6 keeps for the linker. So this MBID is one
    of S4's unnamed ones: a stub beside the name-only artist, which S13 and Merge settle (D2)."""
    root = tmp_path / "library"
    catalog = PgArtistRepository(conn)
    name_only = catalog.upsert_local_artist("A", "a")
    conn.commit()
    for number in range(3):
        _flac(root / f"{number}.flac", number, artist=["A"], musicbrainz_artistid=[ONE])
    _unpaired(root / "several.flac", 9)

    full_scan(conn, root)

    stub = catalog.get_by_mbid(ONE)
    assert stub is not None
    assert (stub.name, stub.normalized_name) == (ONE, None)
    held = catalog.get_by_id(name_only)
    assert held is not None
    assert held.mbid is None


def test_a_single_mbid_alone_stays_out_of_the_sweep(conn: Conn, tmp_path: Path) -> None:
    """Its spelling counts only for an MBID a file holding several also holds."""
    root = tmp_path / "library"
    _flac(root / "single.flac", 1, artist=["C"], musicbrainz_artistid=[THREE])
    _paired(root / "several.flac")

    full_scan(conn, root)

    assert PgArtistRepository(conn).get_by_mbid(THREE) is None


# --- S8 on the real tables --------------------------------------------------------------------


def _tagged_row(conn: Conn, name: str, tag: str) -> None:
    conn.execute(
        """INSERT INTO library_files
               (file_path, format, artist_name, normalized_artist_name, artist_mbid, file_status)
           VALUES (%s, 'flac', %s, %s, %s, 'present')""",
        (f"/music/{uuid4().hex}.flac", name, normalize_artist(name), tag),
    )


@pytest.mark.parametrize(
    ("tags", "promotes"),
    [((), False), ((ONE,), True), ((TWO,), False), ((ONE, TWO), False)],
    ids=["no tags", "only this MBID", "another MBID", "several"],
)
def test_a_further_credit_promotes_only_on_exactly_this_mbid(
    conn: Conn, tags: tuple[str, ...], promotes: bool
) -> None:
    """S8, D5: narrower than S6. With no tag at all it inserts, and the name-only artist stays."""
    catalog = PgArtistRepository(conn)
    name_only = catalog.upsert_local_artist("Foo", "foo")
    for tag in tags:
        _tagged_row(conn, "Foo", tag)

    found = catalog.upsert_further_credit(ONE, "Foo", "Foo, The", "foo")

    assert (found == name_only) is promotes
    held = catalog.get_by_id(name_only)
    assert held is not None
    assert (held.mbid == ONE) is promotes
    made = catalog.get_by_mbid(ONE)
    assert made is not None
    assert (made.name, made.sort_name, made.normalized_name) == ("Foo", "Foo, The", "foo")


def test_a_further_credit_finds_its_mbids_artist(conn: Conn) -> None:
    catalog = PgArtistRepository(conn)
    held = catalog.upsert_musicbrainz_artist(ONE, "Foo", "Foo", "foo")

    assert catalog.upsert_further_credit(ONE, "Another Spelling", "Spelling", "foo") == held
    artist = catalog.get_by_id(held)
    assert artist is not None
    assert artist.name == "Foo"


def test_a_further_credit_never_returns_another_mbids_artist(conn: Conn) -> None:
    catalog = PgArtistRepository(conn)
    other = catalog.upsert_musicbrainz_artist(TWO, "Foo", "Foo", "foo")

    assert catalog.upsert_further_credit(ONE, "Foo", "Foo", "foo") != other
