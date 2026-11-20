"""The work page rebuilt around performances (artist model phase 3b PR 5a; SPEC S8, D41, C71,
C72).

``GET /api/v1/library/works/{id}`` lists the work's performances, each with its credit, a
"default" mark on the master's, its copies and the lines each copy holds through a merge pin
(D41 a, d), the work's credited artists (g), the acts it holds when it holds two or more (f),
and other works by their credited artists (h). It keeps its recordings, which the override and
version panels pick files from. Two sync writes go through services (C75): **Prefer this
copy** sets a stored performance's preferred copy (c), and **Release** frees lines from their
pins (d, e). PUT master keeps its locked body; before it replaces a copy that holds pins, the
page asks which to release (C72, e), so the detail lists those pins first.

The read routes left the router for a reader and a service (D12 item 5): the router runs no
SQL. Every seed is plain SQL through ``credit_picks_seed``; a pin is the column a merge sets
(``matches.pinned_file_id``, 0053).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from backend.routers.library.works import router
from tests.integration import credit_picks_seed as cp

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[dict[str, Any]]

WORKS = "/api/v1/library/works"
ROOT = Path(__file__).resolve().parents[2]
ROUTER = ROOT / "backend" / "routers" / "library" / "works.py"
MAIN_TSX = ROOT / "frontend" / "src" / "main.tsx"


# --- seeds --------------------------------------------------------------------------------------


def pin(conn: Conn, identity_id: UUID, file_id: UUID) -> None:
    """The line's best match pins *file_id*, as a merge leaves it (3b S3)."""
    conn.execute(
        "UPDATE matches SET pinned_file_id = %s WHERE id = %s",
        (file_id, cp.best_match(conn, identity_id)),
    )


def prefer(conn: Conn, performance_id: UUID, file_id: UUID) -> None:
    conn.execute(
        "UPDATE performances SET preferred_copy_id = %s WHERE id = %s", (file_id, performance_id)
    )


def pins_of(conn: Conn, identity_id: UUID) -> set[UUID | None]:
    rows = conn.execute(
        "SELECT pinned_file_id FROM matches WHERE identity_id = %s", (identity_id,)
    ).fetchall()
    return {None if r["pinned_file_id"] is None else UUID(str(r["pinned_file_id"])) for r in rows}


class Sugar:
    """Def Leppard's "Pour Some Sugar On Me" after a merge: the kept master (flac) and the
    merged-away work's master (an mp3), one performance; the merged-away work's line still
    plays the mp3 through its pin, three plays on KISW (AOR)."""

    def __init__(self, conn: Conn) -> None:
        self.leppard = cp.artist(conn, "Def Leppard")
        self.work = cp.work(conn, "Pour Some Sugar On Me", self.leppard)
        self.master = cp.copy(conn, self.work, title="Pour Some Sugar On Me")
        self.merged = cp.copy(conn, self.work, fmt="mp3", title="Pour Some Sugar On Me")
        self.performance = cp.performance(
            conn, self.work, [self.master, self.merged], credit=[self.leppard]
        )
        cp.master(conn, self.work, self.master)
        self.kisw = cp.station(conn, "KISW", "AOR")
        name = cp.log_name(conn, "DEF LEPPARD", self.leppard)
        self.line = cp.line(conn, "Pour Some Sugar On Me", self.master, name=name)
        cp.plays(conn, self.line, 3, station_id=self.kisw)
        pin(conn, self.line, self.merged)


def detail(client: TestClient, work_id: str) -> dict[str, Any]:
    response = client.get(f"{WORKS}/{work_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def performance_of(body: dict[str, Any], performance_id: UUID) -> dict[str, Any]:
    found = [p for p in body["performances"] if p["id"] == str(performance_id)]
    assert len(found) == 1, body["performances"]
    return found[0]


def copy_of(body: dict[str, Any], file_id: UUID) -> dict[str, Any]:
    found = [
        c for p in body["performances"] for c in p["copies"] if c["file"]["id"] == str(file_id)
    ]
    assert len(found) == 1, body["performances"]
    return found[0]


def release(client: TestClient, work_id: str, *identity_ids: UUID) -> Any:
    return client.post(
        f"{WORKS}/{work_id}/pins/release", json={"identity_ids": [str(i) for i in identity_ids]}
    )


def set_master(client: TestClient, work_id: str, file_id: UUID) -> None:
    response = client.put(f"{WORKS}/{work_id}/master", json={"preferred_file_id": str(file_id)})
    assert response.status_code == 200, response.text


def prefer_copy(client: TestClient, work_id: str, performance_id: UUID, file_id: UUID) -> Any:
    return client.put(
        f"{WORKS}/{work_id}/performances/{performance_id}/preferred-copy",
        json={"file_id": str(file_id)},
    )


# --- S8, D41 (a): performances -> copies, each with its credit ---------------------------------


def test_the_detail_lists_each_performance_with_its_credit_and_copies(
    client: TestClient, db_conn: Conn
) -> None:
    duet = cp.Duet(db_conn)
    db_conn.commit()

    body = detail(client, duet.work)

    solo = performance_of(body, duet.solo)
    both = performance_of(body, duet.duet)
    assert [c["name"] for c in solo["credit"]] == ["Bon Jovi"]
    assert solo["credit"][0]["id"] == duet.bon_jovi
    assert both["credit_text"] == "Bon Jovi, Jennifer Nettles"
    assert [c["file"]["id"] for c in solo["copies"]] == [str(duet.master)]
    assert {c["file"]["id"] for c in both["copies"]} == {str(duet.duet_mp3), str(duet.duet_flac)}
    assert both["version_type"] == "original"


def test_the_masters_performance_is_the_default_and_comes_first(
    client: TestClient, db_conn: Conn
) -> None:
    duet = cp.Duet(db_conn)
    db_conn.commit()

    body = detail(client, duet.work)

    assert [(p["id"], p["default"]) for p in body["performances"]] == [
        (str(duet.solo), True),
        (str(duet.duet), False),
    ]


def test_set_master_on_another_performances_copy_makes_it_the_default(
    client: TestClient, db_conn: Conn
) -> None:
    """C71: Set master picks a copy, and with it the default performance, whose preferred copy
    becomes the chosen one when it has one."""
    duet = cp.Duet(db_conn)
    prefer(db_conn, duet.duet, duet.duet_mp3)
    db_conn.commit()

    set_master(client, duet.work, duet.duet_flac)

    body = detail(client, duet.work)
    assert performance_of(body, duet.duet)["default"] is True
    assert performance_of(body, duet.solo)["default"] is False
    assert performance_of(body, duet.duet)["preferred_copy_id"] == str(duet.duet_flac)


def test_the_detail_keeps_its_recordings(client: TestClient, db_conn: Conn) -> None:
    """The override and version panels pick a file from the recordings; they stay."""
    duet = cp.Duet(db_conn)
    db_conn.commit()

    body = detail(client, duet.work)

    files = {f["id"] for r in body["recordings"] for f in r["files"]}
    assert files == {str(duet.master), str(duet.duet_mp3), str(duet.duet_flac)}


def test_a_work_with_no_performance_yet_lists_none(client: TestClient, db_conn: Conn) -> None:
    """Before 3a's build (at the cutover) a work's files have no performance; the page shows
    them from the recordings."""
    work = cp.work(db_conn, "Bother", cp.artist(db_conn, "Stone Sour"))
    cp.copy(db_conn, work, title="Bother")
    db_conn.commit()

    body = detail(client, work)

    assert body["performances"] == []
    assert body["acts"] == []
    assert sum(len(r["files"]) for r in body["recordings"]) == 1


# --- D41 (g), (h): credited artists ------------------------------------------------------------


def test_the_header_names_the_works_credited_artists(client: TestClient, db_conn: Conn) -> None:
    """The default performance's artists first, then the rest, each once."""
    duet = cp.Duet(db_conn)
    db_conn.commit()

    body = detail(client, duet.work)

    assert body["artists"] == [
        {"id": duet.bon_jovi, "name": "Bon Jovi"},
        {"id": duet.nettles, "name": "Jennifer Nettles"},
    ]


def test_an_mbid_only_credit_is_named_without_an_id(client: TestClient, db_conn: Conn) -> None:
    """3a's D1: a credit by MBID alone names no artist row; it shows by its MBID."""
    work = cp.work(db_conn, "Unknown Track", cp.artist(db_conn, "Somebody"))
    mbid = str(uuid4())
    held = cp.performance(db_conn, work, [cp.copy(db_conn, work)], by_mbid=[mbid])
    db_conn.commit()

    body = detail(client, work)

    assert performance_of(body, held)["credit"] == [{"id": None, "name": mbid}]


def test_another_works_copy_names_that_works_credited_artists(
    client: TestClient, db_conn: Conn
) -> None:
    """A copy the page shows through a shared recording names the work it is on by that work's
    credited artists."""
    labelle = cp.artist(db_conn, "LaBelle")
    here = cp.work(db_conn, "Lady Marmalade", labelle)
    there = cp.work(db_conn, "Lady Marmalade", cp.artist(db_conn, "Christina Aguilera"))
    aguilera = cp.artist(db_conn, "Christina Aguilera (2001)")
    recording = str(uuid4())
    db_conn.execute(
        "INSERT INTO recordings (id, title, work_id) VALUES (%s, 'Lady Marmalade', %s)",
        (recording, here),
    )
    on_here = cp.copy(db_conn, here, title="Lady Marmalade")
    on_there = cp.copy(db_conn, there, title="Lady Marmalade")
    db_conn.execute(
        "UPDATE library_files SET recording_id = %s WHERE id = ANY(%s)",
        (recording, [on_here, on_there]),
    )
    cp.performance(db_conn, here, [on_here], credit=[labelle])
    cp.performance(db_conn, there, [on_there], credit=[aguilera])
    db_conn.commit()

    body = detail(client, here)

    shown = [f for r in body["recordings"] for f in r["files"] if f["id"] == str(on_there)]
    assert len(shown) == 1
    assert shown[0]["other_work"]["id"] == there
    assert shown[0]["other_work"]["artists"] == ["Christina Aguilera (2001)"]


# --- D41 (d): pinned lines on each copy, with Release -------------------------------------------


def test_each_copy_lists_the_lines_it_holds_through_a_pin(
    client: TestClient, db_conn: Conn
) -> None:
    sugar = Sugar(db_conn)
    db_conn.commit()

    body = detail(client, sugar.work)

    assert copy_of(body, sugar.master)["pins"] == []
    assert copy_of(body, sugar.merged)["pins"] == [
        {
            "identity_id": str(sugar.line),
            "station_id": str(sugar.kisw),
            "station": "KISW",
            "title": "Pour Some Sugar On Me",
            "artist": "DEF LEPPARD",
            "plays": 3,
            "next_file_id": str(sugar.master),
            "next_path": copy_of(body, sugar.master)["file"]["file_path"],
        }
    ]


def test_a_released_line_plays_what_the_page_said_it_would(
    client: TestClient, db_conn: Conn
) -> None:
    sugar = Sugar(db_conn)
    db_conn.commit()
    assert cp.resolved(db_conn, sugar.line) == {(sugar.merged, "pin")}

    response = release(client, sugar.work, sugar.line)

    assert response.status_code == 200, response.text
    assert response.json() == {"released": 1}
    assert pins_of(db_conn, sugar.line) == {None}
    assert cp.resolved(db_conn, sugar.line) == {(sugar.master, "master")}
    assert copy_of(detail(client, sugar.work), sugar.merged)["pins"] == []


def test_a_pin_an_override_shadows_says_it_keeps_playing_the_override(
    client: TestClient, db_conn: Conn
) -> None:
    """A format override is read before the pin (C61): at such a station the line already plays
    the override, so Release changes nothing there."""
    sugar = Sugar(db_conn)
    kzok = cp.station(db_conn, "KZOK", "Classic Hits")
    override = cp.copy(db_conn, sugar.work, fmt="mp3", title="Pour Some Sugar On Me (Edit)")
    db_conn.execute(
        "UPDATE library_files SET performance_id = %s WHERE id = %s",
        (sugar.performance, override),
    )
    cp.override(db_conn, sugar.work, "Classic Hits", override)
    cp.plays(db_conn, sugar.line, 2, station_id=kzok)
    db_conn.commit()

    pins = copy_of(detail(client, sugar.work), sugar.merged)["pins"]

    by_station = {p["station"]: (p["plays"], p["next_file_id"]) for p in pins}
    assert by_station == {"KISW": (3, str(sugar.master)), "KZOK": (2, str(override))}


def test_release_frees_only_the_lines_it_names(client: TestClient, db_conn: Conn) -> None:
    sugar = Sugar(db_conn)
    other = cp.line(db_conn, "Pour Some Sugar On Me (Remix)", sugar.master)
    cp.plays(db_conn, other, station_id=sugar.kisw)
    pin(db_conn, other, sugar.merged)
    db_conn.commit()

    release(client, sugar.work, sugar.line)

    assert pins_of(db_conn, sugar.line) == {None}
    assert pins_of(db_conn, other) == {sugar.merged}
    assert [
        p["identity_id"] for p in copy_of(detail(client, sugar.work), sugar.merged)["pins"]
    ] == [str(other)]


def test_release_is_logged(client: TestClient, db_conn: Conn) -> None:
    """As D4's releases are: the line then takes the normal path, and the log says why."""
    sugar = Sugar(db_conn)
    db_conn.commit()

    with capture_logs() as events:
        release(client, sugar.work, sugar.line)

    released = [e for e in events if e["event"] == "pins_released"]
    assert len(released) == 1
    assert released[0]["reason"] == "released on the work page"
    assert released[0]["work_id"] == sugar.work
    assert released[0]["lines"] == 1


def test_release_refuses_a_line_that_pins_no_copy_of_the_work(
    client: TestClient, db_conn: Conn
) -> None:
    """Nothing is released when one named line pins no copy of this work."""
    sugar = Sugar(db_conn)
    unpinned = cp.line(db_conn, "Photograph", sugar.master)
    db_conn.commit()

    response = release(client, sugar.work, sugar.line, unpinned)

    assert response.status_code == 422
    assert str(unpinned) in response.json()["detail"]
    assert pins_of(db_conn, sugar.line) == {sugar.merged}


def test_release_of_an_unknown_work_is_not_found(client: TestClient, db_conn: Conn) -> None:
    sugar = Sugar(db_conn)
    db_conn.commit()

    assert release(client, f"work-{uuid4()}", sugar.line).status_code == 404
    assert pins_of(db_conn, sugar.line) == {sugar.merged}


def test_release_names_at_least_one_line(client: TestClient, db_conn: Conn) -> None:
    sugar = Sugar(db_conn)
    db_conn.commit()

    assert release(client, sugar.work).status_code == 422


# --- C72, D41 (e): Set master and the pins of the copy it replaces -----------------------------


class Replaced:
    """A work whose master is a copy a line is pinned to: the pin reads as the master (a pin on
    the master's own file takes the normal path), until Set master replaces that copy."""

    def __init__(self, conn: Conn) -> None:
        self.act = cp.artist(conn, "Heart")
        self.work = cp.work(conn, "Alone", self.act)
        self.old = cp.copy(conn, self.work, title="Alone")
        self.new = cp.copy(conn, self.work, fmt="mp3", title="Alone")
        cp.performance(conn, self.work, [self.old, self.new], credit=[self.act])
        cp.master(conn, self.work, self.old)
        station = cp.station(conn, "KJR", "AOR")
        self.line = cp.line(conn, "Alone", self.old)
        cp.plays(conn, self.line, 4, station_id=station)
        pin(conn, self.line, self.old)


def test_set_master_lists_replaced_pins(client: TestClient, db_conn: Conn) -> None:
    """C72: the detail lists the pins of the master's copy, the copy Set master replaces, so
    the page can ask before releasing any. Today such a line plays the master."""
    world = Replaced(db_conn)
    db_conn.commit()
    assert cp.resolved(db_conn, world.line) == {(world.old, "master")}

    body = detail(client, world.work)

    assert body["song_master"]["preferred_file_id"] == str(world.old)
    pins = copy_of(body, world.old)["pins"]
    assert [(p["identity_id"], p["plays"], p["next_file_id"]) for p in pins] == [
        (str(world.line), 4, str(world.old))
    ]


def test_a_pin_kept_through_set_master_stays_on_the_replaced_copy(
    client: TestClient, db_conn: Conn
) -> None:
    """Unticked in the dialog: PUT master alone (its locked body) leaves the line on the old
    copy, through its pin."""
    world = Replaced(db_conn)
    db_conn.commit()

    set_master(client, world.work, world.new)

    assert cp.resolved(db_conn, world.line) == {(world.old, "pin")}
    assert copy_of(detail(client, world.work), world.old)["pins"][0]["next_file_id"] == str(
        world.new
    )


def test_a_pin_released_before_set_master_follows_the_new_master(
    client: TestClient, db_conn: Conn
) -> None:
    """Ticked in the dialog: the page releases it, then sets the master; releasing first
    changes nothing until the master moves, since the pin reads as the master."""
    world = Replaced(db_conn)
    db_conn.commit()

    assert release(client, world.work, world.line).status_code == 200
    assert cp.resolved(db_conn, world.line) == {(world.old, "master")}
    set_master(client, world.work, world.new)

    assert cp.resolved(db_conn, world.line) == {(world.new, "master")}


# --- D41 (c): Prefer this copy, on a performance some line stores -------------------------------


def stored_duet(conn: Conn) -> tuple[cp.Duet, UUID]:
    """The Duet world with one duet line storing the duet, whose preferred copy is the flac."""
    duet = cp.Duet(conn)
    prefer(conn, duet.duet, duet.duet_flac)
    line = duet.duet_line(conn)
    cp.store(conn, line, duet.duet)
    return duet, line


def test_a_stored_performance_counts_the_lines_that_play_it(
    client: TestClient, db_conn: Conn
) -> None:
    duet, line = stored_duet(db_conn)
    duet.solo_line(db_conn)
    db_conn.commit()
    assert cp.resolved(db_conn, line) == {(duet.duet_flac, "performance")}

    body = detail(client, duet.work)

    assert performance_of(body, duet.duet)["lines"] == 1
    assert performance_of(body, duet.solo)["lines"] == 0
    assert performance_of(body, duet.duet)["preferred_copy_id"] == str(duet.duet_flac)


def test_prefer_this_copy_moves_the_lines_that_store_it(client: TestClient, db_conn: Conn) -> None:
    duet, line = stored_duet(db_conn)
    db_conn.commit()

    response = prefer_copy(client, duet.work, duet.duet, duet.duet_mp3)

    assert response.status_code == 204, response.text
    assert cp.preferred_of(db_conn, duet.duet) == duet.duet_mp3
    assert cp.resolved(db_conn, line) == {(duet.duet_mp3, "performance")}


def test_prefer_refuses_the_default_performance(client: TestClient, db_conn: Conn) -> None:
    """Set master picks the default performance's copy (C71); this route never does."""
    duet, _ = stored_duet(db_conn)
    second = cp.copy(db_conn, duet.work, fmt="mp3", title="Who Says You Can't Go Home")
    db_conn.execute(
        "UPDATE library_files SET performance_id = %s WHERE id = %s", (duet.solo, second)
    )
    db_conn.commit()

    response = prefer_copy(client, duet.work, duet.solo, second)

    assert response.status_code == 422
    assert "Set master" in response.json()["detail"]
    assert cp.preferred_of(db_conn, duet.solo) is None


def test_prefer_refuses_a_copy_of_another_performance(client: TestClient, db_conn: Conn) -> None:
    duet, _ = stored_duet(db_conn)
    db_conn.commit()

    response = prefer_copy(client, duet.work, duet.duet, duet.master)

    assert response.status_code == 422
    assert cp.preferred_of(db_conn, duet.duet) == duet.duet_flac


def test_prefer_refuses_a_missing_copy(client: TestClient, db_conn: Conn) -> None:
    duet, _ = stored_duet(db_conn)
    db_conn.execute(
        "UPDATE library_files SET file_status = 'missing' WHERE id = %s", (duet.duet_mp3,)
    )
    db_conn.commit()

    response = prefer_copy(client, duet.work, duet.duet, duet.duet_mp3)

    assert response.status_code == 422
    assert cp.preferred_of(db_conn, duet.duet) == duet.duet_flac


def test_prefer_refuses_a_performance_of_another_work(client: TestClient, db_conn: Conn) -> None:
    duet, _ = stored_duet(db_conn)
    elsewhere = cp.work(db_conn, "Always", duet.bon_jovi)
    db_conn.commit()

    response = prefer_copy(client, elsewhere, duet.duet, duet.duet_mp3)

    assert response.status_code == 404
    assert cp.preferred_of(db_conn, duet.duet) == duet.duet_flac


# --- D41 (f): a work holding two acts ----------------------------------------------------------


def test_a_work_holding_two_acts_lists_them_and_the_lines_each_reaches(
    client: TestClient, db_conn: Conn
) -> None:
    """An override or version entry reaches only lines on the default path (C61: a stored
    performance plays first): here the solo line, at its AOR station; the duet line keeps the
    duet."""
    duet, _ = stored_duet(db_conn)
    duet.solo_line(db_conn)
    db_conn.commit()

    body = detail(client, duet.work)

    assert body["acts"] == [
        {"credit": "Bon Jovi", "default": True, "lines": 1, "reached": {"AOR": 1}},
        {"credit": "Bon Jovi, Jennifer Nettles", "default": False, "lines": 1, "reached": {}},
    ]


def test_one_act_in_several_performances_lists_no_acts(client: TestClient, db_conn: Conn) -> None:
    act = cp.artist(db_conn, "Metallica")
    work = cp.work(db_conn, "One", act)
    studio = cp.copy(db_conn, work, title="One")
    live = cp.copy(db_conn, work, title="One (Live)")
    cp.performance(db_conn, work, [studio], credit=[act])
    cp.performance(db_conn, work, [live], credit=[act])
    cp.master(db_conn, work, studio)
    db_conn.commit()

    assert detail(client, work)["acts"] == []


# --- C75, D12 item 5: the routes ---------------------------------------------------------------


def test_the_new_writes_are_sync_routes() -> None:
    """Like every write on the works router (``test_works_write_routes_are_sync.py`` holds them
    all to SyncRepos), they run on the request's one connection."""
    paths = {
        "/works/{work_id}/performances/{performance_id}/preferred-copy": "PUT",
        "/works/{work_id}/pins/release": "POST",
    }
    found = {
        route.path: route
        for route in router.routes
        if isinstance(route, APIRoute) and route.path in paths
    }
    assert sorted(found) == sorted(paths)
    for path, method in paths.items():
        assert method in found[path].methods
        assert not inspect.iscoroutinefunction(found[path].endpoint), path


def test_the_works_router_runs_no_sql() -> None:
    """The read routes moved to a reader and a service (design §8: no ``.execute`` left in
    ``routers/library/works.py``)."""
    source = ROUTER.read_text(encoding="utf-8")
    assert re.findall(r"\.execute\(", source) == []
    assert "AsyncConnection" not in source


def test_the_work_page_has_its_own_address_and_keeps_the_old_one() -> None:
    """D41 (b): ``/library/works/:work_id``, beside the artist's ``.../works/:work_id``, both
    the work page (the frontend's router, ``main.tsx``)."""
    source = MAIN_TSX.read_text(encoding="utf-8")
    pages = dict(re.findall(r'path:\s*"([^"]+)",\s*element:\s*<(\w+) />', source))
    assert pages.get("library/works/:work_id") == "AssociatedWorks"
    assert pages.get("library/artists/:artist_id/works/:work_id") == "AssociatedWorks"
