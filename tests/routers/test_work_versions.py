"""The work page's versions (versions phase 2b PR 3, SPEC S5, S11, S12, E3, E5; design M30, C62,
C64, C66; AUD-R034, AUD-R046).

``GET /library/works/{id}/versions`` lists each version text seen on the work's matched lines, and
each text of a version entry the work holds, with:

- its stations, plays, and first-seen date and station (the wall-clock date of its earliest play);
- its state: mapped (a version entry), else dismissed, else unmapped;
- the mapped file, the "new" marker (S8, ``test_unmapped_versions.py``), and any suggestion;
- its log reading (``read_log_version``) and whether that reads as a version. A text that reads as
  title words or as the original never reads as a version (the lock request's question C).

Versions come first, then the other texts, each by plays, most first. The work detail carries the
same list and the master flag. A matched line is the view's: auto or manual matched, its best
match with a file, that file's own work, else its recording's.

Writes:

- ``POST .../version-entries {version_texts, file_id}`` maps one or more listed texts to a
  present file of the work, the same file rule as format overrides (E3). A repeat changes nothing
  (E5), and mapping a mapped text moves it. Each text's state becomes seen now, so a dismissal is
  cleared.
- ``DELETE .../version-entries {version_texts}`` unmaps; states stay.
- ``POST .../versions/dismiss {version_texts}`` dismisses ("the master is fine"), seen now; a
  mapped text is refused, and nothing is written.

S11 (C64, AUD-R046): a suggestion is shown, never applied. An unmapped version suggests the one
copy of that version its lines are matched to, else the work's one copy of it. A copy is a present
file of the work (its own ``work_id``) whose recording is of the version the text reads as.

S12 (C66): the master flag shows when the master file's title reads live, acoustic, demo, remix
or extended, and the work holds a plain copy: a present file of the work, not the master, whose
title has no version text, or reads as title words, the original or a remaster. It is a flag
only.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from backend.domain.synthetic_work_id import encode as encode_synthetic_work_id
from tests.routers import version_pages_seed as seed
from tests.routers.version_pages_seed import AFTER, T0

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.routers.version_pages_seed import Conn

pytestmark = pytest.mark.integration

WORKS = "/api/v1/library/works"


def versions(client: TestClient, work_id: str) -> list[dict[str, Any]]:
    response = client.get(f"{WORKS}/{work_id}/versions")
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()["versions"]
    return listed


def by_text(client: TestClient, work_id: str) -> dict[str, dict[str, Any]]:
    return {v["version_text"]: v for v in versions(client, work_id)}


def map_texts(client: TestClient, work_id: str, texts: list[str], file_id: UUID) -> int:
    response = client.post(
        f"{WORKS}/{work_id}/version-entries",
        json={"version_texts": texts, "file_id": str(file_id)},
    )
    return response.status_code


def at(year: int, month: int, day: int, hour: int = 8, minute: int = 0) -> datetime:
    """A ``played_at``: station wall clock, stored under the UTC label."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- S5: the list -----------------------------------------------------------------------------


def test_lists_each_text_on_matched_lines(client: TestClient, db_conn: Conn) -> None:
    """S5: one row per text on the work's matched lines, its plays and stations summed over
    every such line, its first play's wall-clock date and station; versions, then title words,
    each by plays. Lines matched elsewhere, not matched, or plain add nothing."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    seed.master(db_conn, work, master)
    live = seed.file(db_conn, work, version_type="live")
    kisw = seed.station(db_conn, "KISW")
    wxrt = seed.station(db_conn, "WXRT")
    sm = seed.line(db_conn, "Enter Sandman (Live S&M)", master)
    seed.plays(db_conn, sm, kisw, at(2001, 5, 3), at(2003, 1, 1))
    seed.plays(db_conn, sm, wxrt, at(2000, 1, 1, 0, 30))
    sm_again = seed.line(db_conn, "ENTER SANDMAN  (live  s&m)", master)
    seed.plays(db_conn, sm_again, kisw, at(2004, 6, 1))
    lv = seed.line(db_conn, "Enter Sandman (Lv)", live)
    seed.plays(db_conn, lv, kisw, at(2010, 2, 2))
    seed.line(db_conn, "Enter Sandman (Unplugged)", master)  # matched, never played
    part = seed.line(db_conn, "Enter Sandman (Part 1)", master)
    seed.plays(db_conn, part, kisw, at(2005, 1, 1), at(2005, 1, 2), at(2005, 1, 3))
    plain = seed.line(db_conn, "Enter Sandman", master)
    seed.plays(db_conn, plain, kisw, at(2006, 1, 1))
    other = seed.work(db_conn)
    seed.plays(
        db_conn,
        seed.line(db_conn, "Enter Sandman (Live)", seed.file(db_conn, other)),
        kisw,
        at(2007, 1, 1),
    )
    seed.plays(
        db_conn,
        seed.line(db_conn, "Enter Sandman (Demo)", master, status="needs_review"),
        kisw,
        at(2007, 1, 1),
    )
    elsewhere = seed.line(
        db_conn, "Enter Sandman (Acoustic)", seed.file(db_conn, other), confidence=0.95
    )
    seed.also_match(db_conn, elsewhere, master, confidence=0.5)
    seed.plays(db_conn, elsewhere, kisw, at(2007, 1, 1))
    db_conn.commit()

    listed = versions(client, work)

    assert [(v["version_text"], v["plays"]) for v in listed] == [
        ("live s&m", 4),
        ("lv", 1),
        ("unplugged", 0),
        ("part 1", 3),
    ]
    rows = {v["version_text"]: v for v in listed}
    assert rows["live s&m"]["stations"] == ["KISW", "WXRT"]
    assert (rows["live s&m"]["first_seen"], rows["live s&m"]["first_station"]) == (
        "2000-01-01",
        "WXRT",
    )
    assert (rows["lv"]["stations"], rows["lv"]["first_seen"]) == (["KISW"], "2010-02-02")
    assert (rows["unplugged"]["stations"], rows["unplugged"]["first_seen"]) == ([], None)
    assert rows["unplugged"]["first_station"] is None
    assert [(v["version_type"], v["reads_as_version"]) for v in listed] == [
        ("live", True),
        ("live", True),
        ("acoustic", True),
        ("unknown", False),
    ]
    assert all((v["state"], v["mapped_file_id"]) == ("unmapped", None) for v in listed)
    # No state rows: each version is new (S8); title words never are.
    assert [v["new"] for v in listed] == [True, True, True, False]


def test_lines_through_their_recording(client: TestClient, db_conn: Conn) -> None:
    """A line's work is its file's own work, else the work of the file's recording."""
    work = seed.work(db_conn)
    by_recording = seed.file(db_conn, None, recording_work=work)
    owned_elsewhere = seed.file(db_conn, seed.work(db_conn), recording_work=work)
    seed.line(db_conn, "Fade To Black (Live)", by_recording)
    seed.line(db_conn, "Fade To Black (Demo)", owned_elsewhere)
    db_conn.commit()

    assert [v["version_text"] for v in versions(client, work)] == ["live"]


def test_originals_are_not_versions(client: TestClient, db_conn: Conn) -> None:
    """Question C: a text the log reader reads as the original is listed with the title words;
    "Amended" reads clean, a version."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    seed.line(db_conn, "One (Album Version)", master)
    seed.line(db_conn, "One (Amended)", master)
    db_conn.commit()

    assert [
        (v["version_text"], v["version_type"], v["reads_as_version"])
        for v in versions(client, work)
    ] == [
        ("amended", "clean", True),
        ("album version", "original", False),
    ]


def test_states(client: TestClient, db_conn: Conn) -> None:
    """S5: mapped (an entry, its file named), else dismissed, else unmapped. A text of an entry
    with no matched line left (D1 leaves entries behind) is listed, mapped, with no plays. A
    mapped or dismissed text is never new, even with a copy indexed after its state (and a
    mapped one with no state row at all, C62: mapping clears the marker)."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    live = seed.file(db_conn, work, version_type="live", indexed_at=AFTER)
    seed.file(db_conn, work, version_type="acoustic", indexed_at=AFTER)
    for title in ("Battery (Live)", "Battery (Acoustic)", "Battery (Demo)"):
        seed.line(db_conn, title, master)
    seed.entry(db_conn, work, "live", live)
    seed.entry(db_conn, work, "bbc", live)
    seed.state(db_conn, work, "live")
    seed.state(db_conn, work, "acoustic", "dismissed")
    seed.state(db_conn, work, "demo")
    seed.override(db_conn, work, "AOR", master)
    db_conn.commit()

    rows = by_text(client, work)

    assert {t: (v["state"], v["mapped_file_id"]) for t, v in rows.items()} == {
        "live": ("mapped", str(live)),
        "bbc": ("mapped", str(live)),
        "acoustic": ("dismissed", None),
        "demo": ("unmapped", None),
    }
    assert (rows["bbc"]["plays"], rows["bbc"]["stations"]) == (0, [])
    assert not any(v["new"] for v in rows.values())


def test_work_detail_carries_the_list(client: TestClient, db_conn: Conn) -> None:
    """The work detail gains the same versions and master flag (optional fields, empty for a
    synthetic work), and its format overrides stay format rows only."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work, title="Sad But True (Live)")
    seed.master(db_conn, work, master)
    seed.file(db_conn, work, title="Sad But True")
    seed.line(db_conn, "Sad But True (Live)", master)
    seed.entry(db_conn, work, "live", master)
    seed.override(db_conn, work, "AOR", master)
    db_conn.commit()

    detail = client.get(f"{WORKS}/{work}").json()
    route = client.get(f"{WORKS}/{work}/versions").json()
    synthetic = encode_synthetic_work_id("a-synthetic", "Nothing Else Matters")
    seed.file(db_conn, None, title="Nothing Else Matters")
    db_conn.execute("UPDATE library_files SET artist_mbid = 'a-synthetic' WHERE work_id IS NULL")
    db_conn.commit()
    synthetic_detail = client.get(f"{WORKS}/{synthetic}").json()

    assert detail["versions"] == route["versions"]
    assert detail["master_flag"] == route["master_flag"] is not None
    assert [o["format_name"] for o in detail["format_overrides"]] == ["AOR"]
    assert (synthetic_detail["versions"], synthetic_detail["master_flag"]) == ([], None)


def test_unknown_and_synthetic_works(client: TestClient) -> None:
    """A work that doesn't exist is 404; a synthetic one (no work row, so no lines) lists none,
    or is 404 as its work detail is when no file carries it."""
    synthetic = encode_synthetic_work_id("a-synthetic", "Orion")

    assert client.get(f"{WORKS}/no-such-work/versions").status_code == 404
    response = client.get(f"{WORKS}/{synthetic}/versions")
    assert response.status_code == 404 or response.json() == {
        "versions": [],
        "master_flag": None,
    }


# --- S5: map, unmap, dismiss ------------------------------------------------------------------


def test_map_one_or_more_texts(client: TestClient, db_conn: Conn) -> None:
    """S5, M30: one request maps several texts to one file ("(Live)" and "(Lv)" share it); each
    is then mapped and seen now, a dismissal cleared, and its lines play the file."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    seed.master(db_conn, work, master)
    live = seed.file(db_conn, work, version_type="live")
    lv = seed.line(db_conn, "Whiplash (Lv)", master)
    seed.plays(db_conn, lv, seed.station(db_conn, "KISW"), at(2001, 1, 1))
    seed.line(db_conn, "Whiplash (Live)", master)
    seed.line(db_conn, "Whiplash (Demo)", master)
    seed.state(db_conn, work, "live", "dismissed")
    db_conn.commit()
    before = seed.now(db_conn)

    status = map_texts(client, work, ["live", "lv"], live)

    assert status == 204
    assert seed.entries_of(db_conn, work) == {"live": live, "lv": live}
    states = seed.states_of(db_conn, work)
    assert set(states) == {"live", "lv"}
    assert all(kind is None and seen_at >= before for kind, seen_at in states.values())
    rows = by_text(client, work)
    assert {t: (v["state"], v["mapped_file_id"]) for t, v in rows.items()} == {
        "live": ("mapped", str(live)),
        "lv": ("mapped", str(live)),
        "demo": ("unmapped", None),
    }
    assert seed.resolved(db_conn, lv) == {(live, "version")}


def test_title_words_can_be_mapped(client: TestClient, db_conn: Conn) -> None:
    """A text that reads as title words can still be mapped: a venue the log reader doesn't know,
    such as "(Woodstock 99)", reads so (M30 maps any text the stations wrote)."""
    work = seed.work(db_conn)
    live = seed.file(db_conn, work, version_type="live")
    seed.line(db_conn, "Freak On A Leash (Woodstock 99)", seed.file(db_conn, work))
    db_conn.commit()

    assert map_texts(client, work, ["woodstock 99"], live) == 204
    row = by_text(client, work)["woodstock 99"]
    assert (row["reads_as_version"], row["state"]) == (False, "mapped")


def test_map_twice(client: TestClient, db_conn: Conn) -> None:
    """E5: Map pressed twice is one entry per text, the same file."""
    work = seed.work(db_conn)
    live = seed.file(db_conn, work, version_type="live")
    seed.line(db_conn, "Creeping Death (Live)", live)
    db_conn.commit()

    first = map_texts(client, work, ["live"], live)
    second = map_texts(client, work, ["live"], live)

    assert (first, second) == (204, 204)
    rows = db_conn.execute(
        "SELECT preferred_file_id FROM format_overrides WHERE work_id = %s", (work,)
    ).fetchall()
    assert [UUID(str(r["preferred_file_id"])) for r in rows] == [live]


def test_mapping_a_mapped_text_moves_it(client: TestClient, db_conn: Conn) -> None:
    """Mapping a mapped text to another file of the work points its entry there."""
    work = seed.work(db_conn)
    first = seed.file(db_conn, work, version_type="live")
    better = seed.file(db_conn, work, version_type="live")
    seed.line(db_conn, "Creeping Death (Live)", first)
    seed.entry(db_conn, work, "live", first)
    db_conn.commit()

    assert map_texts(client, work, ["live"], better) == 204
    assert seed.entries_of(db_conn, work) == {"live": better}


def test_file_must_be_on_work(client: TestClient, db_conn: Conn) -> None:
    """E3: the file is a present file whose own work is this one (the format overrides' rule):
    else 422, and nothing is written. An unknown work is 404."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    seed.line(db_conn, "Ride The Lightning (Live)", master)
    other_work = seed.work(db_conn)
    theirs = seed.file(db_conn, other_work, version_type="live")
    on_our_recording = seed.file(db_conn, other_work, version_type="live", recording_work=work)
    missing = seed.file(db_conn, work, version_type="live", status="missing")
    db_conn.commit()

    for file_id in (theirs, on_our_recording, missing, uuid4()):
        assert map_texts(client, work, ["live"], file_id) == 422, file_id
    assert map_texts(client, "no-such-work", ["live"], master) == 404
    assert seed.entries_of(db_conn, work) == {}
    assert seed.states_of(db_conn, work) == {}


def test_texts_must_be_listed(client: TestClient, db_conn: Conn) -> None:
    """Map and Dismiss take only texts the work lists, at least one: else 422, nothing written."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    seed.line(db_conn, "Fight Fire With Fire (Live)", master)
    seed.line(db_conn, "Fight Fire With Fire (Demo)", master, status="needs_review")
    db_conn.commit()

    for texts in (["acoustic"], ["live", "demo"], []):
        assert map_texts(client, work, texts, master) == 422, texts
        dismissed = client.post(f"{WORKS}/{work}/versions/dismiss", json={"version_texts": texts})
        assert dismissed.status_code == 422, texts
    assert seed.entries_of(db_conn, work) == {}
    assert seed.states_of(db_conn, work) == {}


def test_unmap(client: TestClient, db_conn: Conn) -> None:
    """S5: unmapping deletes the text's entry, only that one; its state stays, the line plays the
    master again, and a text that isn't mapped is left as it is."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    seed.master(db_conn, work, master)
    live = seed.file(db_conn, work, version_type="live")
    event_line = seed.line(db_conn, "Master Of Puppets (Live)", master)
    seed.plays(db_conn, event_line, seed.station(db_conn, "KISW"), at(2002, 2, 2))
    seed.line(db_conn, "Master Of Puppets (Lv)", master)
    seed.line(db_conn, "Master Of Puppets (Demo)", master)
    seed.entry(db_conn, work, "live", live)
    seed.entry(db_conn, work, "lv", live)
    seed.override(db_conn, work, "CHR", live)  # not KISW's format, so the master plays
    seed.state(db_conn, work, "live")
    db_conn.commit()

    response = client.request(
        "DELETE", f"{WORKS}/{work}/version-entries", json={"version_texts": ["live", "demo"]}
    )

    assert response.status_code == 204
    assert seed.entries_of(db_conn, work) == {"lv": live}
    assert seed.states_of(db_conn, work) == {"live": (None, T0)}
    assert by_text(client, work)["live"]["state"] == "unmapped"
    assert seed.resolved(db_conn, event_line) == {(master, "master")}
    assert db_conn.execute(
        "SELECT count(*) AS n FROM format_overrides WHERE format_name = 'CHR'"
    ).fetchone() == {"n": 1}


def test_dismiss(client: TestClient, db_conn: Conn) -> None:
    """S5: dismissing marks each text dismissed, seen now; a mapped text is refused (409) and the
    request writes nothing."""
    work = seed.work(db_conn)
    master = seed.file(db_conn, work)
    live = seed.file(db_conn, work, version_type="live")
    for title in ("Orion (Live)", "Orion (Demo)", "Orion (Acoustic)"):
        seed.line(db_conn, title, master)
    seed.entry(db_conn, work, "live", live)
    seed.state(db_conn, work, "acoustic", None, T0)
    db_conn.commit()
    before = seed.now(db_conn)

    refused = client.post(
        f"{WORKS}/{work}/versions/dismiss", json={"version_texts": ["demo", "live"]}
    )
    assert refused.status_code == 409
    assert seed.states_of(db_conn, work) == {"acoustic": (None, T0)}

    done = client.post(
        f"{WORKS}/{work}/versions/dismiss", json={"version_texts": ["demo", "acoustic"]}
    )

    assert done.status_code == 204
    states = seed.states_of(db_conn, work)
    assert {t: kind for t, (kind, _) in states.items()} == {
        "demo": "dismissed",
        "acoustic": "dismissed",
    }
    assert all(seen_at >= before for _, seen_at in states.values())
    assert {t: v["state"] for t, v in by_text(client, work).items()} == {
        "live": "mapped",
        "demo": "dismissed",
        "acoustic": "dismissed",
    }


# --- S11: suggestions -------------------------------------------------------------------------


def test_suggestions_never_applied(client: TestClient, db_conn: Conn) -> None:
    """S11, C64, AUD-R046: an unmapped version suggests the one copy its lines are matched to,
    else the work's one copy of that version; reading the page writes nothing, and the line plays
    the master until Lance maps it."""
    one_copy = seed.work(db_conn)
    master = seed.file(db_conn, one_copy)
    seed.master(db_conn, one_copy, master)
    copy = seed.file(db_conn, one_copy, version_type="live", title="Disposable Heroes (Live)")
    seed.file(db_conn, one_copy, version_type="live", status="missing")
    seed.file(db_conn, seed.work(db_conn), version_type="live", recording_work=one_copy)
    plays_master = seed.line(db_conn, "Disposable Heroes (Live)", master)
    seed.plays(db_conn, plays_master, seed.station(db_conn, "KISW"), at(2003, 3, 3))
    seed.line(db_conn, "Disposable Heroes (Part 2)", master)

    matched = seed.work(db_conn)
    first = seed.file(db_conn, matched, version_type="live")
    seed.file(db_conn, matched, version_type="live")
    seed.line(db_conn, "Damage, Inc. (Live)", first)

    matched_only = seed.work(db_conn)  # its one copy is also the matched file: matched first
    only = seed.file(db_conn, matched_only, version_type="live")
    seed.line(db_conn, "Battery (Live)", only)

    two_copies = seed.work(db_conn)
    two_master = seed.file(db_conn, two_copies)
    seed.file(db_conn, two_copies, version_type="live")
    seed.file(db_conn, two_copies, version_type="live")
    seed.line(db_conn, "Leper Messiah (Live)", two_master)

    dismissed = seed.work(db_conn)
    seed.file(db_conn, dismissed, version_type="live")
    seed.line(db_conn, "The Thing That Should Not Be (Live)", seed.file(db_conn, dismissed))
    seed.state(db_conn, dismissed, "live", "dismissed")

    borrowed = seed.work(db_conn)  # its one copy keeps another work's recording: still its copy
    borrowed_master = seed.file(db_conn, borrowed)
    kept = seed.file(db_conn, borrowed, version_type="live", recording_work=seed.work(db_conn))
    seed.line(db_conn, "Orion (Live)", borrowed_master)
    db_conn.commit()
    states_before = seed.all_states(db_conn)

    one = by_text(client, one_copy)
    picked = by_text(client, matched)["live"]["suggestion"]

    assert one["live"]["suggestion"] == {
        "file_id": str(copy),
        "reason": "only_copy",
        "track_title": "Disposable Heroes (Live)",
        "file_path": seed.path_of(db_conn, copy),
    }
    assert one["part 2"]["suggestion"] is None
    assert (picked["file_id"], picked["reason"]) == (str(first), "matched_file")
    by_match = by_text(client, matched_only)["live"]["suggestion"]
    assert (by_match["file_id"], by_match["reason"]) == (str(only), "matched_file")
    assert by_text(client, two_copies)["live"]["suggestion"] is None
    assert by_text(client, dismissed)["live"]["suggestion"] is None
    lent = by_text(client, borrowed)["live"]["suggestion"]
    assert (lent["file_id"], lent["reason"]) == (str(kept), "only_copy")
    assert db_conn.execute(
        "SELECT count(*) AS n FROM format_overrides WHERE version_text IS NOT NULL"
    ).fetchone() == {"n": 0}
    assert seed.all_states(db_conn) == states_before
    assert seed.resolved(db_conn, plays_master) == {(master, "master")}


# --- S12: the master flag ---------------------------------------------------------------------


def test_master_flag(client: TestClient, db_conn: Conn) -> None:
    """S12, C66: the flag names the master, its reading and the plain copies; a flag only. A
    remaster is a plain copy (the studio take); a work with no master has no flag."""
    flagged = seed.work(db_conn)
    live_master = seed.file(db_conn, flagged, version_type="live", title="Loser (Live)")
    seed.master(db_conn, flagged, live_master)
    plain = seed.file(db_conn, flagged, title="Loser")
    remaster = seed.file(db_conn, flagged, title="Loser (Remastered 2011)")
    seed.file(db_conn, flagged, title="Loser (Acoustic)")
    seed.file(db_conn, flagged, title="Loser", status="missing")
    seed.file(db_conn, seed.work(db_conn), title="Loser", recording_work=flagged)
    event_line = seed.line(db_conn, "Loser", live_master)
    seed.plays(db_conn, event_line, seed.station(db_conn, "KISW"), at(2001, 1, 1))

    no_plain = seed.work(db_conn)
    demo_master = seed.file(db_conn, no_plain, title="Kryptonite (Demo)")
    seed.master(db_conn, no_plain, demo_master)
    seed.file(db_conn, no_plain, title="Kryptonite (Live)")

    plain_master = seed.work(db_conn)
    studio = seed.file(db_conn, plain_master, title="Be Like That")
    seed.master(db_conn, plain_master, studio)
    seed.file(db_conn, plain_master, title="Be Like That (Live)")

    album_version = seed.work(db_conn)
    album = seed.file(db_conn, album_version, title="Here Without You (Album Version)")
    seed.master(db_conn, album_version, album)
    seed.file(db_conn, album_version, title="Here Without You")

    no_master = seed.work(db_conn)
    seed.file(db_conn, no_master, title="Duality (Live)")
    seed.file(db_conn, no_master, title="Duality")
    db_conn.commit()

    flag = client.get(f"{WORKS}/{flagged}/versions").json()["master_flag"]

    assert flag == {
        "master_file_id": str(live_master),
        "version_type": "live",
        "plain_file_ids": sorted([str(plain), str(remaster)]),
    }
    for work in (no_plain, plain_master, album_version, no_master):
        assert client.get(f"{WORKS}/{work}/versions").json()["master_flag"] is None, work
    assert seed.resolved(db_conn, event_line) == {(live_master, "master")}
    assert seed.all_states(db_conn) == set()


@pytest.mark.parametrize(
    ("title", "reading"),
    [
        ("Loser (Unplugged)", "acoustic"),
        ("Loser (Demo)", "demo"),
        ("Loser (Remix)", "remix"),
        ("Loser (Extended)", "extended"),
        ("Loser (Lv)", "live"),
    ],
)
def test_master_flag_readings(client: TestClient, db_conn: Conn, title: str, reading: str) -> None:
    """The five readings that flag a master, by the log reader (C63)."""
    work = seed.work(db_conn)
    taken = seed.file(db_conn, work, title=title)
    seed.master(db_conn, work, taken)
    seed.file(db_conn, work, title="Loser")
    db_conn.commit()

    flag = client.get(f"{WORKS}/{work}/versions").json()["master_flag"]

    assert flag is not None and flag["version_type"] == reading
