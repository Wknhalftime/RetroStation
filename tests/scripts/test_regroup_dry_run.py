"""Phase 2b S13 (C63, D4): ``scripts/regroup_dry_run.py``, what the reader fix would regroup.

    python scripts/regroup_dry_run.py --dsn <bench copy> --before <checkout> --out <file>

The fix changes how new and re-scanned files group; files already grouped keep their work and
recording. Before it merges, Lance sees on the gate's copy (AUD-R044) what it would change. The
dry run reads the copy once and writes one JSON document:

- ``header``: the gate tools' header (tool, format version, the copy's marker).
- ``files``: every present file, by id: its ``path``, ``title`` and ``artist`` tags, and its
  reading (``base_title``, ``version_type``) by the reader of the ``--before`` checkout
  (``before``) and by this tree's (``after``).
- ``differs``: the files whose work or recording grouping would give them (were they new, or
  re-scanned without a work) differs between the two readings, by id: for each side, the
  ``work_id`` (null when grouping would create a work), the ``work_title`` (that work's, or the
  one it would create) and the recording's ``version_type`` (grouping keys a recording by work
  and version type). A file grouping places by a shortcut (its audio's twin, its recording MBID
  already on a work) reads no title, so it is never listed.
- ``recordings``: the MusicBrainz recordings (a present file's recording MBID tag names them)
  whose stored version is the before reading of their title and whose after reading differs:
  the next enrichment upsert writes the new version. By id: ``title``, ``work_id``, ``stored``,
  ``after``, and ``slot_taken``: another recording on the work holds ``after``, or another
  listed recording on it is going there too (``uq_recordings_work_version``: an upsert fails).
- ``summary``: the counts (``files``, ``differs``, ``recordings``, ``slot_taken`` among them).

It prints a line for each listed file and recording, refuses a database without "bench" in its
name, and writes nothing.

The "before" reader here stands in for the reader before the fix: this tree's, except for
three of the misreads C63 fixes. The seeds follow the gate tests' rule: the library comes from
the scan. The MusicBrainz recordings are inserted as today's reader left them, which the fixed
code no longer writes, and the audio twins get their hash as the hash backfill writes it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import sql as pg_sql

from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash
from backend.services.repository_factory import RepositoryFactory
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import retag
from tests.scripts.gate_world import (
    ROOT,
    Conn,
    decoy_dsn,
    file_named,
    load_tool,
    run_guarded,
    scan,
    tagged,
    work_of,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.usefixtures("queue_in_memory"),
]

_BEFORE_READER = '''\
"""A stand-in for the reader before C63: this tree's, but with three of its old misreads."""

from backend.domain.enums import VersionType
from backend.services.normalization import extract_version_info as _this_trees

_MISREADS = {
    " (Album Version)": VersionType.REMIX,
    " (Amended)": VersionType.REMIX,
    " (I'm Special)": VersionType.EDITION,
}


def extract_version_info(raw_title: str) -> tuple[str, VersionType]:
    for suffix, version in _MISREADS.items():
        if raw_title.endswith(suffix):
            return raw_title[: -len(suffix)], version
    return _this_trees(raw_title)
'''


def _before_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "before"
    reader = checkout / "backend" / "services" / "normalization.py"
    reader.parent.mkdir(parents=True)
    reader.write_text(_BEFORE_READER, encoding="utf-8")
    return checkout


def _dry_run(bench: str, before: Path, out: Path) -> tuple[dict[str, Any], str]:
    done = run_guarded(
        bench, "regroup_dry_run.py", "--dsn", bench, "--before", str(before), "--out", str(out)
    )
    assert done.returncode == 0, done.stdout + done.stderr
    doc: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return doc, done.stdout


def _mb_recording(conn: Conn, work_id: str | None, title: str, version: str) -> str:
    """A MusicBrainz recording as enrichment stored it with the reader before C63."""
    recording_id = str(uuid4())
    conn.execute(
        "INSERT INTO recordings (id, title, work_id, version_type) VALUES (%s, %s, %s, %s)",
        (recording_id, title, work_id, version),
    )
    conn.commit()
    return recording_id


def _same_audio(conn: Conn, *names: str) -> None:
    """The files get one audio hash, as the hash backfill records it."""
    audio = AudioHash(AudioHashKind.AUDIO_SHA256, uuid4().hex * 2)
    repos = RepositoryFactory(conn)
    for name in names:
        file_id = file_named(conn, name)
        row = conn.execute(
            "SELECT file_size, file_mtime_ns FROM library_files WHERE id = %s", (file_id,)
        ).fetchone()
        assert row is not None
        assert repos.library_files.set_audio_hash(
            file_id, audio, int(row["file_size"]), int(row["file_mtime_ns"])
        )
    conn.commit()


@dataclass(frozen=True)
class _Library:
    root: Path
    gone: UUID
    listed: dict[str, str]  # file name -> title, the files the dry run must list


def _library(conn: Conn, root: Path) -> _Library:
    """The plain songs are scanned first, so the labelled copies group onto them, as they did
    on the dev library. gone.mp3 is scanned, deleted and rescanned: a missing file."""
    tagged(root / "a" / "kid.mp3", "The Pretenders", "Kid")
    tagged(root / "a" / "brass.mp3", "The Pretenders", "Brass In Pocket")
    tagged(root / "a" / "hole.mp3", "Alice In Chains", "Down In A Hole")
    tagged(root / "a" / "would.mp3", "Alice In Chains", "Would?")
    tagged(root / "a" / "hey.mp3", "OutKast", "Hey Ya!")
    tagged(root / "a" / "gone.mp3", "Alice In Chains", "Would? (Album Version)")
    scan(conn, root)
    gone = file_named(conn, "gone.mp3")
    (root / "a" / "gone.mp3").unlink()
    labelled = [
        ("hole-album.mp3", "Alice In Chains", "Down In A Hole (Album Version)"),
        ("hey-amended.mp3", "OutKast", "Hey Ya! (Amended)"),
        ("brass-special.mp3", "The Pretenders", "Brass in Pocket (I'm Special)"),
        ("kid-special.mp3", "The Pretenders", "Kid (I'm Special)"),
    ]
    for name, artist, title in labelled:
        tagged(root / "b" / name, artist, title)
    listed = {name: title for name, _, title in labelled}
    tagged(root / "b" / "hole-live.mp3", "Alice In Chains", "Down In A Hole (Live)")
    # The hash twin of hole.mp3: grouping takes its twin's work, whatever its title reads.
    tagged(root / "b" / "twin.mp3", "Alice In Chains", "Down In A Hole (Album Version)")
    # A recording MBID that is no shortcut: unknown here, or on no work. Grouping reads these
    # files' titles, so they are listed.
    tagged(
        root / "b" / "unknown-mbid.mp3",
        "Alice In Chains",
        "Would? (Album Version)",
        recording_mbid=str(uuid4()),
    )
    tagged(
        root / "b" / "workless.mp3",
        "OutKast",
        "Hey Ya! (Amended)",
        recording_mbid=seed.recording(conn, None),
    )
    # No artist tag: grouping leaves it ungrouped whatever its title reads.
    tagged(root / "b" / "no-artist.mp3", "", "Would? (Album Version)")
    conn.commit()
    scan(conn, root)
    _same_audio(conn, "hole.mp3", "twin.mp3")
    listed["unknown-mbid.mp3"] = "Would? (Album Version)"
    listed["workless.mp3"] = "Hey Ya! (Amended)"
    return _Library(root, gone, listed)


def _fingerprint(conn: Conn) -> dict[str, tuple[int, str]]:
    """Every table's row count and a digest of its rows."""
    tables = [
        str(r["tablename"])
        for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    ]
    out: dict[str, tuple[int, str]] = {}
    for table in tables:
        row = conn.execute(
            pg_sql.SQL(
                """SELECT count(*) AS n,
                          md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) AS digest
                   FROM {} t"""
            ).format(pg_sql.Identifier(table))
        ).fetchone()
        assert row is not None
        out[table] = (int(row["n"]), str(row["digest"]))
    conn.commit()
    return out


def _work_title(conn: Conn, work_id: str) -> str:
    row = conn.execute("SELECT title FROM works WHERE id = %s", (work_id,)).fetchone()
    assert row is not None
    return str(row["title"])


def _side(entry: dict[str, Any], side: str) -> tuple[Any, Any, Any]:
    placed = entry[side]
    return placed["work_id"], placed["work_title"], placed["version_type"]


@pytest.fixture
def library(conn: Conn, tool_env: None, tmp_path: Path) -> Iterator[_Library]:
    yield _library(conn, tmp_path / "music")


# --- every present file's reading -----------------------------------------------------------------


def test_every_present_file_has_both_readings(
    conn: Conn, bench: str, library: _Library, tmp_path: Path
) -> None:
    load_tool("gate").mark_copy(bench, source="retrostation", copied_at="2026-11-07T08:00:00+00:00")

    doc, _ = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    assert doc["header"]["tool"] == "regroup_dry_run"
    assert doc["header"]["format_version"] == 1
    assert doc["header"]["copy"]["copied_at"] == "2026-11-07T08:00:00+00:00"
    present = {
        str(r["id"])
        for r in conn.execute("SELECT id FROM library_files WHERE file_status = 'present'")
    }
    assert set(doc["files"]) == present
    assert str(library.gone) not in doc["files"]
    assert doc["summary"]["files"] == len(present)
    hole_album = doc["files"][str(file_named(conn, "hole-album.mp3"))]
    assert hole_album["title"] == "Down In A Hole (Album Version)"
    assert hole_album["artist"] == "Alice In Chains"
    assert Path(hole_album["path"]).name == "hole-album.mp3"
    assert hole_album["before"] == {"base_title": "Down In A Hole", "version_type": "remix"}
    assert hole_album["after"] == {"base_title": "Down In A Hole", "version_type": "original"}
    special = doc["files"][str(file_named(conn, "brass-special.mp3"))]
    assert special["before"] == {"base_title": "Brass in Pocket", "version_type": "edition"}
    assert special["after"] == {
        "base_title": "Brass in Pocket (I'm Special)",
        "version_type": "original",
    }
    live = doc["files"][str(file_named(conn, "hole-live.mp3"))]
    both = {"base_title": "Down In A Hole", "version_type": "live"}
    assert live["before"] == live["after"] == both
    plain = doc["files"][str(file_named(conn, "kid.mp3"))]
    assert plain["before"] == plain["after"] == {"base_title": "Kid", "version_type": "original"}


# --- the files that would group differently -------------------------------------------------------


def test_the_files_whose_work_or_recording_would_differ(
    conn: Conn, bench: str, library: _Library, tmp_path: Path
) -> None:
    hole = work_of(conn, file_named(conn, "hole.mp3"))
    would = work_of(conn, file_named(conn, "would.mp3"))
    hey = work_of(conn, file_named(conn, "hey.mp3"))
    brass = work_of(conn, file_named(conn, "brass.mp3"))
    kid = work_of(conn, file_named(conn, "kid.mp3"))
    before_run = _fingerprint(conn)

    doc, stdout = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    assert _fingerprint(conn) == before_run  # it wrote nothing
    ids = {name: str(file_named(conn, name)) for name in library.listed}
    # Not twin.mp3 (its audio's twin places it), not no-artist.mp3 (grouping skips it), not
    # the missing gone.mp3, not the files whose two readings agree.
    assert str(file_named(conn, "no-artist.mp3")) in doc["files"]
    assert set(doc["differs"]) == set(ids.values())
    differs = {name: doc["differs"][file_id] for name, file_id in ids.items()}
    # Retyped: the same work, the recording of the new version.
    assert _side(differs["hole-album.mp3"], "before") == (hole, "Down In A Hole", "remix")
    assert _side(differs["hole-album.mp3"], "after") == (hole, "Down In A Hole", "original")
    assert _side(differs["hey-amended.mp3"], "before") == (hey, "Hey Ya!", "remix")
    assert _side(differs["hey-amended.mp3"], "after") == (hey, "Hey Ya!", "clean")
    assert _side(differs["unknown-mbid.mp3"], "before") == (would, "Would?", "remix")
    assert _side(differs["unknown-mbid.mp3"], "after") == (would, "Would?", "original")
    assert _side(differs["workless.mp3"], "before") == (hey, "Hey Ya!", "remix")
    assert _side(differs["workless.mp3"], "after") == (hey, "Hey Ya!", "clean")
    # Read as a title: "brass in pocket im special" still finds the plain work.
    assert _side(differs["brass-special.mp3"], "before") == (brass, "Brass In Pocket", "edition")
    assert _side(differs["brass-special.mp3"], "after") == (brass, "Brass In Pocket", "original")
    # "kid im special" does not find "Kid": a work of its own, the one the scan made for it or
    # one grouping would create.
    kid_special = differs["kid-special.mp3"]
    assert _side(kid_special, "before") == (kid, "Kid", "edition")
    after_work, after_title, after_version = _side(kid_special, "after")
    assert after_work != kid
    assert (after_title, after_version) == ("Kid (I'm Special)", "original")
    if after_work is not None:
        assert _work_title(conn, after_work) == "Kid (I'm Special)"
    assert doc["summary"]["differs"] == len(ids)
    for title in set(library.listed.values()):
        assert title in stdout


def test_a_new_copy_lands_where_the_dry_run_said(
    conn: Conn, bench: str, library: _Library, tmp_path: Path
) -> None:
    """The prediction is grouping's own: a new copy of each listed file, scanned, takes the
    work and the recording version the ``after`` side named."""
    doc, _ = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")
    copies = {}
    for name, title in library.listed.items():
        file_id = str(file_named(conn, name))
        artist = doc["files"][file_id]["artist"]
        copies[name] = (tagged(library.root / "c" / f"copy-{name}", artist, title), file_id)
    scan(conn, library.root)

    for name, (path, file_id) in copies.items():
        row = conn.execute(
            """SELECT f.work_id, w.title, r.version_type FROM library_files f
               JOIN works w ON w.id = f.work_id JOIN recordings r ON r.id = f.recording_id
               WHERE f.id = %s""",
            (file_named(conn, path.name),),
        ).fetchone()
        assert row is not None, name
        work_id, work_title, version = _side(doc["differs"][file_id], "after")
        assert row["version_type"] == version, name
        assert row["title"] == work_title, name
        if work_id is not None:
            assert row["work_id"] == work_id, name


def test_a_file_placed_by_its_recording_mbid_is_not_listed(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    """Grouping takes the recording's work before it reads a title, so the reading can't move
    the file."""
    recording = _mb_recording(conn, seed.work(conn), "Rooster (Album Version)", "remix")
    tagged(
        tmp_path / "music" / "rooster.mp3",
        "Alice In Chains",
        "Rooster (Album Version)",
        recording_mbid=recording,
    )
    scan(conn, tmp_path / "music")
    rooster = str(file_named(conn, "rooster.mp3"))

    doc, _ = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    assert doc["files"][rooster]["before"]["version_type"] == "remix"
    assert doc["files"][rooster]["after"]["version_type"] == "original"
    assert doc["differs"] == {}


def test_a_work_past_the_fuzzy_matchs_100_candidates_is_found(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    """Grouping's fuzzy match sees an artist name's first 100 works by title; its last check
    finds an exact title past them. The dry run finds it there too, rather than a new work."""
    music = tmp_path / "music"
    for n in range(100):
        # Titles no two of which fuzzy-match, all sorting before "Zebra".
        tagged(music / "a" / f"n{n:03}.mp3", "The Prolific", f"Song {uuid4().hex[:10].upper()}")
    tagged(music / "a" / "zebra.mp3", "The Prolific", "Zebra Crossing")
    scan(conn, music)
    zebra = work_of(conn, file_named(conn, "zebra.mp3"))
    tagged(music / "b" / "zebra-album.mp3", "The Prolific", "Zebra Crossing (Album Version)")
    scan(conn, music)
    album = str(file_named(conn, "zebra-album.mp3"))
    assert work_of(conn, file_named(conn, "zebra-album.mp3")) == zebra  # as grouping placed it

    doc, _ = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    assert _side(doc["differs"][album], "before") == (zebra, "Zebra Crossing", "remix")
    assert _side(doc["differs"][album], "after") == (zebra, "Zebra Crossing", "original")


def test_a_rescan_keeps_a_grouped_files_work_and_recording(
    conn: Conn, tool_env: None, tmp_path: Path
) -> None:
    """S13: existing files keep their work and recording. A file today's reader put on a remix
    recording of its own stays there when it is scanned again, re-read and all."""
    music = tmp_path / "music"
    tagged(music / "hole.mp3", "Alice In Chains", "Down In A Hole")
    tagged(music / "hole-album.mp3", "Alice In Chains", "Down In A Hole (Album Version)")
    scan(conn, music)
    album = file_named(conn, "hole-album.mp3")
    work = work_of(conn, album)
    remix = _mb_recording(conn, work, "Down In A Hole (Album Version)", "remix")
    conn.execute("UPDATE library_files SET recording_id = %s WHERE id = %s", (remix, album))
    conn.commit()
    retag(music / "hole-album.mp3")  # a new size and mtime: the scan reads its tags again

    scan(conn, music)

    row = conn.execute(
        "SELECT work_id, recording_id FROM library_files WHERE id = %s", (album,)
    ).fetchone()
    assert row is not None
    assert (row["work_id"], row["recording_id"]) == (work, remix)


# --- MusicBrainz recordings the next upsert would retype ------------------------------------------


def test_the_recordings_the_next_upsert_would_retype(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    taken = seed.work(conn)
    plain = _mb_recording(conn, taken, "Down In A Hole", "original")
    album = _mb_recording(conn, taken, "Down In A Hole (Album Version)", "remix")
    # The slot's holder is named by no file: it is still there.
    unnamed = seed.work(conn)
    _mb_recording(conn, unnamed, "Would?", "original")
    lp = _mb_recording(conn, unnamed, "Would? (Album Version)", "remix")
    alone = seed.work(conn)
    free = _mb_recording(conn, alone, "Them Bones (Album Version)", "remix")
    clean = seed.work(conn)
    amended = _mb_recording(conn, clean, "Hey Ya! (Amended)", "remix")
    # Two going to one empty slot: whichever upsert comes second fails.
    contested = seed.work(conn)
    nutshell = _mb_recording(conn, contested, "Nutshell (Album Version)", "remix")
    special = _mb_recording(conn, contested, "Nutshell (I'm Special)", "edition")
    # MusicBrainz said live: enrichment writes live again, whatever the title reads.
    live = _mb_recording(conn, seed.work(conn), "Rooster (Album Version)", "live")
    # Named only by a missing file, or by no file: no enrichment writes them again.
    gone = _mb_recording(conn, seed.work(conn), "Grind (Album Version)", "remix")
    _mb_recording(conn, seed.work(conn), "Angry Chair (Album Version)", "remix")
    music = tmp_path / "music"
    named = {
        "plain": plain,
        "album": album,
        "lp": lp,
        "free": free,
        "amended": amended,
        "nutshell": nutshell,
        "special": special,
        "live": live,
        "gone": gone,
    }
    for name, recording in named.items():
        tagged(music / f"{name}.mp3", "Alice In Chains", name, recording_mbid=recording)
    scan(conn, music)
    (music / "gone.mp3").unlink()
    scan(conn, music)

    doc, stdout = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    expected = {
        album: ("Down In A Hole (Album Version)", taken, "remix", "original", True),
        lp: ("Would? (Album Version)", unnamed, "remix", "original", True),
        free: ("Them Bones (Album Version)", alone, "remix", "original", False),
        amended: ("Hey Ya! (Amended)", clean, "remix", "clean", False),
        nutshell: ("Nutshell (Album Version)", contested, "remix", "original", True),
        special: ("Nutshell (I'm Special)", contested, "edition", "original", True),
    }
    assert set(doc["recordings"]) == set(expected)
    for recording_id, (title, work_id, stored, after, slot_taken) in expected.items():
        entry = doc["recordings"][recording_id]
        assert (entry["title"], entry["work_id"], entry["stored"]) == (title, work_id, stored)
        assert (entry["after"], entry["slot_taken"]) == (after, slot_taken), title
        assert title in stdout
    assert doc["summary"]["recordings"] == 6
    assert doc["summary"]["slot_taken"] == 4


# --- the same reader on both sides, and the guards ------------------------------------------------


def test_this_trees_reader_on_both_sides_changes_nothing(
    conn: Conn, bench: str, library: _Library, tmp_path: Path
) -> None:
    doc, _ = _dry_run(bench, ROOT, tmp_path / "regroup.json")

    assert doc["files"]
    assert all(entry["before"] == entry["after"] for entry in doc["files"].values())
    assert doc["differs"] == {}
    assert doc["recordings"] == {}
    assert doc["summary"]["differs"] == 0


def test_it_refuses_a_database_without_bench(bench: str, tmp_path: Path) -> None:
    out = tmp_path / "regroup.json"
    done = run_guarded(
        bench,
        "regroup_dry_run.py",
        "--dsn",
        decoy_dsn(bench),
        "--before",
        str(ROOT),
        "--out",
        str(out),
    )

    assert done.returncode != 0
    assert "refusing to touch non-bench database" in done.stdout + done.stderr
    assert "Traceback" not in done.stdout + done.stderr
    assert not out.exists()


def test_a_checkout_without_a_reader_is_refused(bench: str, tmp_path: Path) -> None:
    out = tmp_path / "regroup.json"
    done = run_guarded(
        bench,
        "regroup_dry_run.py",
        "--dsn",
        bench,
        "--before",
        str(tmp_path / "empty"),
        "--out",
        str(out),
    )

    assert done.returncode != 0
    assert "normalization.py" in done.stdout + done.stderr
    assert "Traceback" not in done.stdout + done.stderr
    assert not out.exists()
