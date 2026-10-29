"""Phase 0b gate (SPEC 2026-10-28): the air-play snapshot and its compare (S1, S2, E1, E4, E5,
E8).

``scripts/airplay_snapshot.py --dsn <db> --out <file>`` writes one sorted JSON file:
``header`` (outside the compared payload), ``sections`` (one dict per section, keyed by the
strings ``--mode`` and allowlists use) and ``timing``. ``--compare <old> <new>`` exits 0 when
every difference is allowed, 1 when one is not, 2 when it refuses to compare; ``--report <file>``
writes what it found as JSON: ``refused`` (a reason or null), ``failed`` and ``allowed`` (lists
of ``{section, key, kind, old, new}``; ``kind`` is ``added``, ``removed``, ``changed`` or
``merged``).

Keys: air-play and work-source ``<station>/<line>``; matches ``<line>``; file-work and tags
``<file>``; masters ``<work>``; overrides ``<work>/<column>=<value>`` for each other key column
that is set (``<work>/format_name=AC`` today); missing-matches ``<station>``; names
``<log name>``; artists ``<artist id>``; timing ``<reader>``. Allowlist lines are
``<section> <key> <reason>``; a line starting with ``#`` is a comment. ``timing`` holds
``pairs`` (how many (station, line) pairs were timed: all of them up to 200), ``sample`` (their
keys, sorted) and ``readers`` (``<reader>: {median_ms}``).

The DB tests seed through the scan, the ingestion task, the matching tasks and the routes
(``gate_world``); the compare tests build their files by hand. One seed is an exception, stated
where it is made: no route or task makes an unnamed stub artist before phase 1a, so
``test_every_section_on_seed`` inserts one into ``artists`` (stable columns only).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from backend.services.normalization import normalize_artist
from tests.integration import stream_seed as seed
from tests.scripts.gate_world import (
    BETA_MBID,
    Conn,
    World,
    build_world,
    catalog_artist,
    file_named,
    load_tool,
    name_id,
    read_json,
    resolve_line,
    resolve_name,
    scan,
    tagged,
    work_of,
    write_json,
)

pytestmark = pytest.mark.usefixtures("queue_in_memory")

SECTIONS = {
    "air-play",
    "work-source",
    "matches",
    "file-work",
    "masters",
    "overrides",
    "missing-matches",
    "tags",
    "names",
    "artists",
}
READERS = {"m3u", "playable_schedule", "stream_cue_work"}


@pytest.fixture(scope="module")
def snapshot_tool() -> Any:
    return load_tool("airplay_snapshot")


def take(tool: Any, dsn: str, out: Path) -> dict[str, Any]:
    assert tool.main(["--dsn", dsn, "--out", str(out)]) == 0
    return read_json(out)


def compare(tool: Any, old: Path, new: Path, tmp_path: Path, *extra: str) -> tuple[int, Any]:
    report = tmp_path / f"report-{uuid4().hex}.json"  # a fresh file: never a stale report
    code = tool.main(["--compare", str(old), str(new), "--report", str(report), *extra])
    return code, read_json(report)


def failed_keys(report: dict[str, Any]) -> set[tuple[str, str]]:
    return {(d["section"], d["key"]) for d in report["failed"]}


def assert_sorted(text: str) -> None:
    """Every object in the file has its keys in sorted order."""

    def check(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        keys = [key for key, _value in pairs]
        assert keys == sorted(keys)
        return dict(pairs)

    json.loads(text, object_pairs_hook=check)


def pair(station: UUID, line: UUID) -> str:
    return f"{station}/{line}"


def delta_station_and_line(conn: Conn) -> tuple[UUID, UUID]:
    row = conn.execute(
        """SELECT pl.station_id, pe.identity_id FROM play_events pe
           JOIN playlists pl ON pl.id = pe.playlist_id
           JOIN track_identities ti ON ti.id = pe.identity_id
           WHERE ti.original_title = 'DELTA SONG'"""
    ).fetchone()
    assert row is not None
    return UUID(str(row["station_id"])), UUID(str(row["identity_id"]))


# --- S1 ----------------------------------------------------------------------------------------


@pytest.mark.integration
def test_every_section_on_seed(
    snapshot_tool: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1: one sorted file; the header outside the payload; every section on the shared world.

    Air play is keyed by (station, line) and holds the file that plays and its status; work and
    source sit in their own section for the same keys. A missing file is in neither file-work
    nor tags; an unnamed stub (name = MBID) is left out of artists.
    """
    world = build_world(conn, client, tmp_path, monkeypatch)
    # The seed rule's one exception here: an unnamed stub can't be made through the app before
    # phase 1a. artists keeps these columns through every phase.
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, mbid, origin)
           VALUES ('stub-0b', '0b000000-0000-4000-8000-00000000057b',
                   '0b000000-0000-4000-8000-00000000057b',
                   '0b000000-0000-4000-8000-00000000057b', 'musicbrainz')"""
    )
    conn.commit()
    delta_station, delta_song = delta_station_and_line(conn)
    out = tmp_path / "snapshot.json"
    data = take(snapshot_tool, bench, out)

    assert_sorted(out.read_text(encoding="utf-8"))
    assert set(data) == {"header", "sections", "timing"}
    header = data["header"]
    assert header["format_version"] == snapshot_tool.FORMAT_VERSION
    assert header["copy"] is None  # the bench database is not a gate copy
    assert {"tool", "format_version", "copy", "commit", "master_commit"} <= set(header)
    s = data["sections"]
    assert set(s) == SECTIONS

    first, second, beta = str(world.album), str(world.second), str(world.beta_file)
    single = str(world.single)
    present = {"file_status": "present"}
    assert s["air-play"] == {
        pair(world.ac, world.first_song): {"file": single, **present},  # the AC override
        pair(world.chr, world.first_song): {"file": first, **present},  # the master
        pair(world.nofmt, world.first_song): {"file": first, **present},
        pair(world.ac, world.second_song): {"file": second, **present},
        pair(world.ac, world.beta_hit): {"file": beta, **present},
        pair(world.ac, world.unknown_song): {"file": None, "file_status": None},
        pair(delta_station, delta_song): {"file": None, "file_status": None},
    }
    first_work, second_work = world.first_work, work_of(conn, world.second)
    beta_work = work_of(conn, world.beta_file)
    assert s["work-source"] == {
        pair(world.ac, world.first_song): {"work": first_work, "source": "override"},
        pair(world.chr, world.first_song): {"work": first_work, "source": "master"},
        pair(world.nofmt, world.first_song): {"work": first_work, "source": "master"},
        pair(world.ac, world.second_song): {"work": second_work, "source": "master"},
        pair(world.ac, world.beta_hit): {"work": beta_work, "source": "master"},
        pair(world.ac, world.unknown_song): {"work": None, "source": None},
        pair(delta_station, delta_song): {"work": None, "source": None},
    }

    matches = s["matches"]
    assert set(matches) == {
        str(line)
        for line in (
            world.first_song,
            world.second_song,
            world.beta_hit,
            world.unknown_song,
            world.unplayed,
            delta_song,
        )
    }
    assert matches[str(world.first_song)]["status"] == "auto_matched"
    assert matches[str(world.first_song)]["file"] in {first, single}  # equal scores: either copy
    assert matches[str(world.second_song)] == {"status": "auto_matched", "file": second}
    assert matches[str(world.beta_hit)] == {"status": "auto_matched", "file": beta}
    assert matches[str(world.unknown_song)] == {"status": "needs_review", "file": None}
    assert matches[str(world.unplayed)] == {"status": "pending", "file": None}
    assert matches[str(delta_song)] == {"status": "pending", "file": None}

    assert s["file-work"] == {
        first: first_work,
        single: first_work,
        second: second_work,
        beta: beta_work,
    }
    masters = s["masters"]
    gone_work = work_of(conn, world.gone)
    assert {k: v for k, v in masters.items() if k != gone_work} == {
        first_work: first,
        second_work: second,
        beta_work: beta,
    }
    assert masters.get(gone_work) in (None, str(world.gone))
    assert s["overrides"] == {f"{first_work}/format_name=AC": single}
    assert s["missing-matches"] == {
        str(world.ac): 1,  # GAMMA / UNKNOWN SONG
        str(world.chr): 0,
        str(world.nofmt): 0,
        str(delta_station): 1,  # DELTA SONG, still pending
    }

    alpha_tags = {
        "artist_name": "Alpha Band",
        "normalized_artist_name": normalize_artist("Alpha Band"),
        "artist_mbid": None,
    }
    assert s["tags"] == {
        first: alpha_tags,
        single: alpha_tags,
        second: alpha_tags,
        beta: {
            "artist_name": "Beta Group",
            "normalized_artist_name": normalize_artist("Beta Group"),
            "artist_mbid": BETA_MBID,
        },
    }

    alpha = catalog_artist(client, "Alpha Band")
    beta_artist = catalog_artist(client, "Beta Group")
    alpha_key = alpha["mbid"] or alpha["id"]
    beta_key = beta_artist["mbid"] or beta_artist["id"]
    lonely = name_id(conn, "LONELY")
    names = s["names"]
    assert set(names) == {
        str(n) for n in (world.alpha, world.beta, world.gamma, world.delta, lonely)
    }
    # A link the matcher made reads "legacy" before phase 1a (from its matches row, as 1a S2
    # copies it) and "automatic" once 1a's writer makes it (1a S6).
    automatic = {"legacy", "automatic"}
    for name, key in ((world.alpha, alpha_key), (world.beta, beta_key)):
        entry = names[str(name)]
        assert (entry["status"], entry["reason"], set(entry["links"])) == (
            "auto_matched",
            None,
            {key},
        )
        assert entry["links"][key] in automatic
    assert names[str(world.gamma)] == {
        "status": "needs_review",
        "reason": "DEFERRED_RETRY",
        "links": {},
    }
    assert names[str(world.delta)] == {
        "status": "manual_matched",
        "reason": None,
        "links": {alpha_key: "manual", BETA_MBID: "manual"},
    }
    assert names[str(lonely)] == {"status": "pending", "reason": None, "links": {}}

    listed = client.get("/api/v1/library/artists", params={"limit": 500}).json()["items"]
    assert s["artists"] == {
        a["id"]: {"name": a["name"], "mbid": a["mbid"], "origin": a["origin"]}
        for a in listed
        if a["name"] != a["mbid"]
    }
    assert "stub-0b" not in s["artists"]

    timing = data["timing"]
    assert timing["pairs"] == len(s["air-play"])  # fewer than 200 on the seed: every pair
    assert timing["sample"] == sorted(s["air-play"])
    assert set(timing["readers"]) == READERS
    for reader in READERS:
        assert timing["readers"][reader]["median_ms"] > 0


@pytest.mark.integration
def test_timing_uses_a_fixed_200_pair_sample(
    snapshot_tool: Any, conn: Conn, bench: str, tmp_path: Path, tool_env: None
) -> None:
    """S1: the timing record times the three fenced readers over 200 (station, line) pairs,
    the same 200 on every run over the same data."""
    playlist = seed.playlist(conn, seed.station(conn, "AC"))
    for minute in range(205):
        line = seed.identity(conn, artist=f"ACT {minute}", title="SONG", status="pending")
        seed.play(conn, playlist, line, seed.at(f"{8 + minute // 60:02d}:{minute % 60:02d}"))
    conn.commit()

    first = take(snapshot_tool, bench, tmp_path / "first.json")
    second = take(snapshot_tool, bench, tmp_path / "second.json")

    timing = first["timing"]
    assert len(first["sections"]["air-play"]) == 205
    assert timing["pairs"] == 200
    assert len(set(timing["sample"])) == 200
    assert set(timing["sample"]) <= set(first["sections"]["air-play"])
    assert second["timing"]["sample"] == timing["sample"]
    for reader in READERS:
        assert timing["readers"][reader]["median_ms"] > 0


@pytest.mark.integration
def test_one_change_per_section_is_found(
    snapshot_tool: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1, S2: a change made through the app shows up in every section, and fails exact compare.

    One new song by a new act (the scan), Set master, a new format override, a line resolved by
    hand and a log name resolved by hand.
    """
    world = build_world(conn, client, tmp_path, monkeypatch)
    old = tmp_path / "old.json"
    take(snapshot_tool, bench, old)

    tagged(tmp_path / "library" / "epsilon.mp3", "Epsilon Act", "Epsilon Song")
    scan(conn, tmp_path / "library")
    epsilon_file = file_named(conn, "epsilon.mp3")
    epsilon_artist = catalog_artist(client, "Epsilon Act")["id"]
    second_work = work_of(conn, world.second)
    master = client.put(
        f"/api/v1/library/works/{world.first_work}/master",
        json={"preferred_file_id": str(world.single)},
    )
    assert master.status_code == 200, master.text
    override = client.post(
        f"/api/v1/library/works/{second_work}/format-overrides",
        json={"format_name": "AC", "preferred_file_id": str(world.second)},
    )
    assert override.status_code == 201, override.text
    resolve_line(client, world.unknown_song, world.beta_file)
    resolve_name(client, world.gamma, world.alpha_artist)
    conn.commit()

    new = tmp_path / "new.json"
    take(snapshot_tool, bench, new)
    code, report = compare(snapshot_tool, old, new, tmp_path)

    assert code == 1
    assert report["refused"] is None
    found = failed_keys(report)
    expected = {
        ("air-play", pair(world.chr, world.first_song)),  # Set master
        ("work-source", pair(world.ac, world.second_song)),  # the new override: source
        ("matches", str(world.unknown_song)),  # resolved by hand
        ("file-work", str(epsilon_file)),  # the new song
        ("masters", world.first_work),  # Set master
        ("overrides", f"{second_work}/format_name=AC"),  # the new override
        ("missing-matches", str(world.ac)),  # UNKNOWN SONG no longer missing
        ("tags", str(epsilon_file)),  # the new song
        ("names", str(world.gamma)),  # GAMMA resolved by hand
        ("artists", epsilon_artist),  # the new act
    }
    assert expected <= found
    assert {section for section, _key in found} == SECTIONS
    kinds = {(d["section"], d["key"]): d["kind"] for d in report["failed"]}
    assert kinds[("file-work", str(epsilon_file))] == "added"
    assert kinds[("masters", world.first_work)] == "changed"
    by_key = {(d["section"], d["key"]): d for d in report["failed"]}
    assert by_key[("missing-matches", str(world.ac))]["old"] == 1
    assert by_key[("missing-matches", str(world.ac))]["new"] == 0


# --- E1, E5 ------------------------------------------------------------------------------------


@pytest.mark.integration
def test_formatless_and_unplayed(
    snapshot_tool: Any,
    conn: Conn,
    client: TestClient,
    bench: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E1: a station with no format plays through the master, whatever the work's overrides; a
    line with no plays has no (station, line) key and shows only under matches."""
    world: World = build_world(conn, client, tmp_path, monkeypatch)
    s = take(snapshot_tool, bench, tmp_path / "snapshot.json")["sections"]

    nofmt = pair(world.nofmt, world.first_song)
    assert s["air-play"][nofmt] == {"file": str(world.album), "file_status": "present"}
    assert s["work-source"][nofmt]["source"] == "master"
    assert s["overrides"]  # the work does have an override, for AC
    unplayed = str(world.unplayed)
    for section in ("air-play", "work-source"):
        assert not [key for key in s[section] if key.endswith(f"/{unplayed}")]
    assert s["matches"][unplayed] == {"status": "pending", "file": None}


@pytest.mark.integration
def test_empty_db(snapshot_tool: Any, bench: str, tmp_path: Path, tool_env: None) -> None:
    """E5: an empty database gives empty sections, no timing pairs, and nothing to compare."""
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    data = take(snapshot_tool, bench, old)
    take(snapshot_tool, bench, new)

    assert data["sections"] == {section: {} for section in SECTIONS}
    assert data["timing"]["pairs"] == 0
    code, report = compare(snapshot_tool, old, new, tmp_path)
    assert code == 0
    assert report == {"refused": None, "failed": [], "allowed": []}


# --- E4, E8 ------------------------------------------------------------------------------------


@pytest.mark.integration
def test_version_and_copy_mismatch_refused(
    snapshot_tool: Any, bench: str, tmp_path: Path, tool_env: None
) -> None:
    """E4: files from different tool versions, or from different copies, are never compared;
    ``gate.mark_copy`` marks a database as a gate copy, as ``clone`` does.

    E8: a recorder's output changes only in its own PR, which bumps its format version. The
    versions are pinned here, so that PR is also the one that relocks this test.
    """
    assert snapshot_tool.FORMAT_VERSION == 1
    assert load_tool("golden_responses").FORMAT_VERSION == 1
    assert load_tool("dry_run_matching").FORMAT_VERSION == 1

    gate = load_tool("gate")
    gate.mark_copy(bench, source="retrostation", copied_at="2026-10-29T08:00:00+00:00")
    first = tmp_path / "first.json"
    data = take(snapshot_tool, bench, first)
    assert data["header"]["copy"]["copied_at"] == "2026-10-29T08:00:00+00:00"
    same = tmp_path / "same.json"
    take(snapshot_tool, bench, same)
    assert compare(snapshot_tool, first, same, tmp_path)[0] == 0

    newer_tool = write_json(
        tmp_path / "newer.json",
        {**data, "header": {**data["header"], "format_version": snapshot_tool.FORMAT_VERSION + 1}},
    )
    code, report = compare(snapshot_tool, first, newer_tool, tmp_path)
    assert code == 2
    assert "version" in report["refused"]

    other_tool = write_json(
        tmp_path / "other.json", {**data, "header": {**data["header"], "tool": "golden_responses"}}
    )
    assert compare(snapshot_tool, first, other_tool, tmp_path)[0] == 2

    gate.mark_copy(bench, source="retrostation", copied_at="2026-10-30T08:00:00+00:00")
    other_copy = tmp_path / "other-copy.json"
    take(snapshot_tool, bench, other_copy)
    code, report = compare(snapshot_tool, first, other_copy, tmp_path)
    assert code == 2
    assert "copy" in report["refused"]
    assert report["failed"] == []


# --- S2: compare modes, allowlists, timing, work maps -------------------------------------------


def _doc(sections: dict[str, dict[str, Any]], medians: dict[str, float | None]) -> dict[str, Any]:
    """A snapshot file as the tool writes it, built by hand."""
    return {
        "header": {
            "tool": "airplay_snapshot",
            "format_version": 1,
            "copy": {"source": "retrostation", "copied_at": "2026-10-29T08:00:00+00:00"},
            "commit": "abc",
            "master_commit": "def",
        },
        "sections": {section: sections.get(section, {}) for section in SECTIONS},
        "timing": {
            "pairs": 200,
            "readers": {r: {"median_ms": medians.get(r, 10.0)} for r in sorted(READERS)},
        },
    }


def _files(
    tmp_path: Path,
    old: dict[str, dict[str, Any]],
    new: dict[str, dict[str, Any]],
    old_timing: dict[str, float | None] | None = None,
    new_timing: dict[str, float | None] | None = None,
) -> tuple[Path, Path]:
    return (
        write_json(tmp_path / "a.json", _doc(old, old_timing or {})),
        write_json(tmp_path / "b.json", _doc(new, new_timing or {})),
    )


def test_compare_modes(snapshot_tool: Any, tmp_path: Path) -> None:
    """S2: exact by default; only-down lets a count fall, never rise; additions lets none become
    a file, never a file become another file or none; an allowlisted key (with a reason)
    passes; timing fails above 2x unless allowlisted."""
    f = {"file_status": "present"}
    old = {
        "air-play": {
            "s/l1": {"file": None, "file_status": None},
            "s/l2": {"file": "f2", **f},
            "s/l3": {"file": "f3", **f},
        },
        "missing-matches": {"s1": 5, "s2": 3, "s3": 1},
    }

    a, b = _files(tmp_path, old, old)
    assert compare(snapshot_tool, a, b, tmp_path) == (
        0,
        {"refused": None, "failed": [], "allowed": []},
    )
    # the header's commits are outside the payload: a base and a branch run differ there
    moved = read_json(b)
    moved["header"] = {**moved["header"], "commit": "other", "master_commit": "other"}
    write_json(b, moved)
    assert compare(snapshot_tool, a, b, tmp_path)[0] == 0

    # exact (the default): any change fails
    a, b = _files(tmp_path, old, {**old, "missing-matches": {"s1": 4, "s2": 3, "s3": 1}})
    code, report = compare(snapshot_tool, a, b, tmp_path)
    assert code == 1
    assert report["failed"] == [
        {"section": "missing-matches", "key": "s1", "kind": "changed", "old": 5, "new": 4}
    ]

    # only-down: falls and a vanished key pass; a rise or a new key fails
    down = {"s1": 4, "s2": 3, "s4": 2}  # s1 falls, s3 vanishes, s4 is new
    a, b = _files(tmp_path, old, {**old, "missing-matches": down})
    code, report = compare(snapshot_tool, a, b, tmp_path, "--mode", "missing-matches=only-down")
    assert code == 1
    assert failed_keys(report) == {("missing-matches", "s4")}
    assert {d["key"] for d in report["allowed"]} == {"s1", "s3"}
    a, b = _files(tmp_path, old, {**old, "missing-matches": {"s1": 6, "s2": 3, "s3": 1}})
    code, report = compare(snapshot_tool, a, b, tmp_path, "--mode", "missing-matches=only-down")
    assert (code, failed_keys(report)) == (1, {("missing-matches", "s1")})

    # additions: none -> a file (or a new key with a file) passes; file -> file or none fails
    added = {
        "s/l1": {"file": "f1", **f},
        "s/l2": {"file": "f9", **f},
        "s/l3": {"file": None, "file_status": None},
        "s/l4": {"file": "f4", **f},
    }
    a, b = _files(tmp_path, old, {**old, "air-play": added})
    code, report = compare(snapshot_tool, a, b, tmp_path, "--mode", "air-play=additions")
    assert code == 1
    assert failed_keys(report) == {("air-play", "s/l2"), ("air-play", "s/l3")}
    assert {d["key"] for d in report["allowed"]} == {"s/l1", "s/l4"}

    # an allowlist passes the keys it names, each with its reason
    allow = tmp_path / "allow.txt"
    allow.write_text(
        "# reviewed by Lance\nair-play s/l2 re-pointed on purpose\n"
        "air-play s/l3 taken off air on purpose\n",
        encoding="utf-8",
    )
    code, report = compare(
        snapshot_tool, a, b, tmp_path, "--mode", "air-play=additions", "--allow", str(allow)
    )
    assert (code, report["failed"]) == (0, [])
    assert {d["key"] for d in report["allowed"]} == {"s/l1", "s/l2", "s/l3", "s/l4"}

    # an allowlist line with no reason is refused
    allow.write_text("air-play s/l2\n", encoding="utf-8")
    code, report = compare(snapshot_tool, a, b, tmp_path, "--allow", str(allow))
    assert code == 2
    assert "reason" in report["refused"]

    # an unknown mode is refused
    code, report = compare(snapshot_tool, a, b, tmp_path, "--mode", "air-play=mostly")
    assert code == 2

    # timing: above 2x fails, unless allowlisted; up to 2x passes
    a, b = _files(tmp_path, old, old, {"m3u": 10.0}, {"m3u": 19.0})
    assert compare(snapshot_tool, a, b, tmp_path)[0] == 0
    a, b = _files(tmp_path, old, old, {"m3u": 10.0}, {"m3u": 25.0})
    code, report = compare(snapshot_tool, a, b, tmp_path)
    assert (code, failed_keys(report)) == (1, {("timing", "m3u")})
    allow.write_text("timing m3u the view gained a lookup (2b, C61)\n", encoding="utf-8")
    assert compare(snapshot_tool, a, b, tmp_path, "--allow", str(allow))[0] == 0


def test_work_map_merged_rows(snapshot_tool: Any, tmp_path: Path) -> None:
    """S2: a work map (old id -> new id) is applied to the work-keyed sections before comparing;
    two old rows landing on one key are reported as merged, with both old values."""
    f = {"file_status": "present"}
    old = {
        "air-play": {"s/l1": {"file": "f1", **f}, "s/l2": {"file": "f2", **f}},
        "work-source": {
            "s/l1": {"work": "w1", "source": "master"},
            "s/l2": {"work": "w2", "source": "master"},
        },
        "file-work": {"f1": "w1", "f2": "w2", "f3": "w1"},
        "masters": {"w1": "f1", "w2": "f2"},
        "overrides": {"w1/format_name=AC": "f3"},
    }
    new = {
        "air-play": old["air-play"],
        "work-source": {
            "s/l1": {"work": "W", "source": "master"},
            "s/l2": {"work": "W", "source": "master"},
        },
        "file-work": {"f1": "W", "f2": "W", "f3": "W"},
        "masters": {"W": "f1"},
        "overrides": {"W/format_name=AC": "f3"},
    }
    a, b = _files(tmp_path, old, new)
    work_map = write_json(tmp_path / "work_map.json", {"w1": "W", "w2": "W"})

    assert compare(snapshot_tool, a, b, tmp_path)[0] == 1  # without the map: many differences
    code, report = compare(snapshot_tool, a, b, tmp_path, "--work-map", str(work_map))
    assert code == 1
    assert report["failed"] == [
        {"section": "masters", "key": "W", "kind": "merged", "old": ["f1", "f2"], "new": "f1"}
    ]

    allow = tmp_path / "allow.txt"
    allow.write_text("masters W the merge keeps the first master (3b)\n", encoding="utf-8")
    code, report = compare(
        snapshot_tool, a, b, tmp_path, "--work-map", str(work_map), "--allow", str(allow)
    )
    assert (code, report["failed"]) == (0, [])
    assert [d["kind"] for d in report["allowed"]] == ["merged"]
