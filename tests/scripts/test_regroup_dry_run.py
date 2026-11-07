"""Phase 2b S13 (C63, D4): ``scripts/regroup_dry_run.py``, what the reader fix would regroup.

    python scripts/regroup_dry_run.py --dsn <bench copy> --before <checkout> --out <file>

The fix changes how new and re-scanned files group; files already grouped keep their work and
recording. Before it merges, Lance sees on the gate's copy (AUD-R044) what it would change. The
dry run reads the copy once and writes one JSON document:

- ``files``: every present file, by id: its path, title and artist tags, and its reading
  (``base_title``, ``version_type``) by the reader of the ``--before`` checkout and by this
  tree's.
- ``differs``: the files whose work or recording grouping would give them differs between the
  two readings, by id: for each side, the ``work_id`` (null when grouping would create a work),
  the ``work_title`` (that work's, or the one it would create) and the recording's
  ``version_type`` (grouping keys a recording by work and version type). A file grouping places
  by a shortcut (its recording MBID already on a work, its audio's twin) reads no title, so it
  is never listed.
- ``recordings``: the MusicBrainz recordings (a file's recording MBID tag names them) whose
  stored version is the before reading of their title and whose after reading differs: the next
  enrichment upsert writes the new version. By id: ``title``, ``work_id``, ``stored``,
  ``after``, and ``slot_taken``, whether another recording on the work already holds ``after``
  (``uq_recordings_work_version``: that upsert would fail).
- ``summary``: the counts, also printed.

It refuses a database without "bench" in its name and writes nothing.

The "before" reader here is a stand-in for the reader before the fix: this tree's, except for
three of the misreads C63 fixes. The seeds follow the gate tests' rule: the library comes from
the scan. The MusicBrainz recordings are inserted as today's reader left them, which the fixed
code no longer writes.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from backend.domain.library import AudioMetadata
from backend.services.repository_factory import RepositoryFactory
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import library_file
from tests.scripts.gate_world import (
    ROOT,
    SCRIPTS,
    Conn,
    decoy_dsn,
    file_named,
    run_guarded,
    scan,
    tagged,
    work_of,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("queue_in_memory")]

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


def _library(conn: Conn, root: Path) -> None:
    """The plain songs are scanned first, so the labelled copies group onto them, as they did
    on the dev library."""
    tagged(root / "a" / "kid.mp3", "The Pretenders", "Kid")
    tagged(root / "a" / "brass.mp3", "The Pretenders", "Brass In Pocket")
    tagged(root / "a" / "hole.mp3", "Alice In Chains", "Down In A Hole")
    tagged(root / "a" / "hey.mp3", "OutKast", "Hey Ya!")
    scan(conn, root)
    tagged(root / "b" / "hole-album.mp3", "Alice In Chains", "Down In A Hole (Album Version)")
    tagged(root / "b" / "hey-amended.mp3", "OutKast", "Hey Ya! (Amended)")
    tagged(root / "b" / "brass-special.mp3", "The Pretenders", "Brass in Pocket (I'm Special)")
    tagged(root / "b" / "kid-special.mp3", "The Pretenders", "Kid (I'm Special)")
    tagged(root / "b" / "hole-live.mp3", "Alice In Chains", "Down In A Hole (Live)")
    scan(conn, root)


def _mb_recording(conn: Conn, work_id: str, title: str, version: str) -> str:
    """A MusicBrainz recording as enrichment stored it with the reader before C63."""
    recording_id = str(uuid4())
    conn.execute(
        "INSERT INTO recordings (id, title, work_id, version_type) VALUES (%s, %s, %s, %s)",
        (recording_id, title, work_id, version),
    )
    conn.commit()
    return recording_id


def _fingerprint(conn: Conn) -> dict[str, tuple[int, str]]:
    """Each table the dry run reads: its row count and a digest of its rows."""
    tables = ("library_files", "works", "recordings", "artists", "song_masters")
    out: dict[str, tuple[int, str]] = {}
    for table in tables:
        row = conn.execute(
            f"""SELECT count(*) AS n,
                       md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) AS digest
                FROM {table} t"""
        ).fetchone()
        assert row is not None
        out[table] = (int(row["n"]), str(row["digest"]))
    return out


def _work_title(conn: Conn, work_id: str) -> str:
    row = conn.execute("SELECT title FROM works WHERE id = %s", (work_id,)).fetchone()
    assert row is not None
    return str(row["title"])


# --- every present file's reading -----------------------------------------------------------------


def test_every_present_file_has_both_readings(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    _library(conn, tmp_path / "music")
    gone = library_file(
        RepositoryFactory(conn),
        str(tmp_path / "music" / "gone.flac"),
        None,
        missing=True,
        audio=AudioMetadata(artist_name="Alice In Chains", track_title="Rooster (Album Version)"),
    )
    conn.commit()

    doc, _ = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    present = {
        str(r["id"])
        for r in conn.execute("SELECT id FROM library_files WHERE file_status = 'present'")
    }
    assert set(doc["files"]) == present
    assert str(gone.id) not in doc["files"]
    hole_album = doc["files"][str(file_named(conn, "hole-album.mp3"))]
    assert hole_album["title"] == "Down In A Hole (Album Version)"
    assert hole_album["artist"] == "Alice In Chains"
    assert hole_album["path"].endswith("hole-album.mp3")
    assert hole_album["before"] == {"base_title": "Down In A Hole", "version_type": "remix"}
    assert hole_album["after"] == {"base_title": "Down In A Hole", "version_type": "original"}
    special = doc["files"][str(file_named(conn, "brass-special.mp3"))]
    assert special["before"] == {"base_title": "Brass in Pocket", "version_type": "edition"}
    assert special["after"] == {
        "base_title": "Brass in Pocket (I'm Special)",
        "version_type": "original",
    }
    live = doc["files"][str(file_named(conn, "hole-live.mp3"))]
    assert (
        live["before"] == live["after"] == {"base_title": "Down In A Hole", "version_type": "live"}
    )
    plain = doc["files"][str(file_named(conn, "kid.mp3"))]
    assert plain["before"] == plain["after"] == {"base_title": "Kid", "version_type": "original"}


# --- the files that would group differently -------------------------------------------------------


def test_the_files_whose_work_or_recording_would_differ(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    _library(conn, tmp_path / "music")
    hole = work_of(conn, file_named(conn, "hole.mp3"))
    hey = work_of(conn, file_named(conn, "hey.mp3"))
    brass = work_of(conn, file_named(conn, "brass.mp3"))
    kid = work_of(conn, file_named(conn, "kid.mp3"))
    before_run = _fingerprint(conn)

    doc, stdout = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    assert _fingerprint(conn) == before_run  # it wrote nothing
    hole_album = str(file_named(conn, "hole-album.mp3"))
    hey_amended = str(file_named(conn, "hey-amended.mp3"))
    brass_special = str(file_named(conn, "brass-special.mp3"))
    kid_special = str(file_named(conn, "kid-special.mp3"))
    assert set(doc["differs"]) == {hole_album, hey_amended, brass_special, kid_special}
    # Retyped: the same work, the recording of the new version.
    assert doc["differs"][hole_album] == {
        "before": {"work_id": hole, "work_title": "Down In A Hole", "version_type": "remix"},
        "after": {"work_id": hole, "work_title": "Down In A Hole", "version_type": "original"},
    }
    assert doc["differs"][hey_amended] == {
        "before": {"work_id": hey, "work_title": "Hey Ya!", "version_type": "remix"},
        "after": {"work_id": hey, "work_title": "Hey Ya!", "version_type": "clean"},
    }
    # Read as a title: "brass in pocket im special" still finds the plain work.
    assert doc["differs"][brass_special] == {
        "before": {"work_id": brass, "work_title": "Brass In Pocket", "version_type": "edition"},
        "after": {"work_id": brass, "work_title": "Brass In Pocket", "version_type": "original"},
    }
    # "kid im special" does not find "Kid": a work of its own, the one the scan made for it or
    # one grouping would create.
    kid_entry = doc["differs"][kid_special]
    assert kid_entry["before"] == {"work_id": kid, "work_title": "Kid", "version_type": "edition"}
    assert kid_entry["after"]["work_id"] != kid
    assert kid_entry["after"]["work_title"] == "Kid (I'm Special)"
    assert kid_entry["after"]["version_type"] == "original"
    if kid_entry["after"]["work_id"] is not None:
        assert _work_title(conn, kid_entry["after"]["work_id"]) == "Kid (I'm Special)"
    for title in (
        "Down In A Hole (Album Version)",
        "Hey Ya! (Amended)",
        "Brass in Pocket (I'm Special)",
        "Kid (I'm Special)",
    ):
        assert title in stdout
    assert doc["summary"]["differs"] == 4


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


# --- MusicBrainz recordings the next upsert would retype ------------------------------------------


def test_the_recordings_the_next_upsert_would_retype(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    taken = seed.work(conn)
    plain = _mb_recording(conn, taken, "Down In A Hole", "original")
    album = _mb_recording(conn, taken, "Down In A Hole (Album Version)", "remix")
    alone = seed.work(conn)
    free = _mb_recording(conn, alone, "Would? (Album Version)", "remix")
    # MusicBrainz said live: enrichment writes live again, whatever the title reads.
    live = _mb_recording(conn, seed.work(conn), "Rooster (Album Version)", "live")
    clean = seed.work(conn)
    amended = _mb_recording(conn, clean, "Hey Ya! (Amended)", "remix")
    # No file names it, so no enrichment writes it again.
    _mb_recording(conn, seed.work(conn), "Them Bones (Album Version)", "remix")
    music = tmp_path / "music"
    for name, recording in [
        ("plain", plain),
        ("album", album),
        ("free", free),
        ("live", live),
        ("amended", amended),
    ]:
        tagged(music / f"{name}.mp3", "Alice In Chains", name, recording_mbid=recording)
    scan(conn, music)

    doc, _ = _dry_run(bench, _before_checkout(tmp_path), tmp_path / "regroup.json")

    assert doc["recordings"] == {
        album: {
            "title": "Down In A Hole (Album Version)",
            "work_id": taken,
            "stored": "remix",
            "after": "original",
            "slot_taken": True,
        },
        free: {
            "title": "Would? (Album Version)",
            "work_id": alone,
            "stored": "remix",
            "after": "original",
            "slot_taken": False,
        },
        amended: {
            "title": "Hey Ya! (Amended)",
            "work_id": clean,
            "stored": "remix",
            "after": "clean",
            "slot_taken": False,
        },
    }
    assert doc["summary"]["recordings"] == 3
    assert doc["summary"]["slot_taken"] == 1


# --- the same reader on both sides, and the guards ------------------------------------------------


def test_this_trees_reader_on_both_sides_changes_nothing(
    conn: Conn, bench: str, tool_env: None, tmp_path: Path
) -> None:
    _library(conn, tmp_path / "music")

    doc, _ = _dry_run(bench, ROOT, tmp_path / "regroup.json")

    assert doc["files"]
    assert all(entry["before"] == entry["after"] for entry in doc["files"].values())
    assert doc["differs"] == {}
    assert doc["recordings"] == {}
    assert doc["summary"]["files"] == len(doc["files"])
    assert doc["summary"]["differs"] == 0


def test_it_refuses_a_database_without_bench(bench: str, tmp_path: Path) -> None:
    decoy = decoy_dsn(bench)
    done = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "regroup_dry_run.py"),
            "--dsn",
            decoy,
            "--before",
            str(ROOT),
            "--out",
            str(tmp_path / "regroup.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert done.returncode != 0
    assert "refusing to touch non-bench database" in done.stdout + done.stderr
    assert not (tmp_path / "regroup.json").exists()


def test_a_checkout_without_a_reader_is_refused(bench: str, tmp_path: Path) -> None:
    done = run_guarded(
        bench,
        "regroup_dry_run.py",
        "--dsn",
        bench,
        "--before",
        str(tmp_path / "empty"),
        "--out",
        str(tmp_path / "regroup.json"),
    )

    assert done.returncode != 0
    assert "normalization.py" in done.stdout + done.stderr
    assert not (tmp_path / "regroup.json").exists()
