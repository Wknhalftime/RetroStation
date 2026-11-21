"""The merge list's "Keep apart" mark (artist model phase 3b PR 5c; SPEC D40, D55, D56, D58, S2,
E4; scope check Q3, Q6-Q9, R3; AUD-R089), on PostgreSQL.

``POST /api/v1/library/merge-list/keep-apart`` takes ``{"work_ids": [...]}``, a group's works as
Lance saw them, and keeps every pair of them apart (D55), merged-away ids followed to the work
they redirect to. A pair already kept keeps its row (its note and time). Each new mark's note is
"Kept apart on the merge list" (Q8). It answers ``{"kept": [{"first_work_id",
"second_work_id"}, ...]}``, every pair of the group in order. A work that neither exists nor
redirects answers 404; fewer than two works answers 422.

``POST /api/v1/library/merge-list/kept-apart/undo`` takes ``{"first_work_id",
"second_work_id"}`` in either order and deletes that one pair: the next load plans the two works
again if they still join (D55). A pair that isn't kept (any more) answers 204 too (Q3).

The page (``GET /api/v1/library/merge-list``) gains two things:

- **``kept_apart``**, every pair, sent whole with each page (Q9), newest first::

      [{"first": {"work_id", "title", "artists"}, "second": {...}, "kept_at", "note",
        "musicbrainz_links"}, ...]

  ``musicbrainz_links`` is true when a work MBID joins the two works, their own or one a cached
  recording lookup of their performances names (Q6, E4: MusicBrainz links them, the mark keeps
  them apart).
- **A held group (D56)**: two kept-apart works still joined through others. Each group carries
  ``held``, the kept pairs inside it (``[{"first_work_id", "second_work_id"}, ...]``, empty for
  a group that isn't held). A held group isn't previewed (D27: no ``changes``), counts in
  ``total`` and keeps its place on its page (D37). Apply never merges it: its works don't plan
  as an applicable group, so a send of it answers ``changed``.

The count (``GET .../count``) gains ``kept_apart``, the number of pairs; Library Status shows its
link while any of the four has rows, the number still the planned groups (D58).

Split and reassign mark their own pair (Q7), through their Library routes too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from tests.integration import credit_picks_seed as cp
from tests.integration import keep_apart_seed as ka
from tests.integration import work_merge_seed as seed
from tests.integration.keep_apart_seed import ThreeTakes
from tests.integration.work_merge_seed import TwoWorks

pytestmark = pytest.mark.integration

PAGE = "/api/v1/library/merge-list"
COUNT = f"{PAGE}/count"
APPLY = f"{PAGE}/apply"
KEEP = f"{PAGE}/keep-apart"
UNDO = f"{PAGE}/kept-apart/undo"

type Conn = psycopg.Connection[dict]


@pytest.fixture
def ready(db_conn: Conn) -> Conn:
    """3a's build and D1's credits count as done (Q22)."""
    cp.mark_built(db_conn)
    cp.set_marker(db_conn, WORK_ARTISTS_CREDITED_SETTING)
    db_conn.commit()
    return db_conn


# --- calls ----------------------------------------------------------------------------------


def page(client: TestClient, offset: int = 0) -> dict[str, Any]:
    response = client.get(PAGE, params={"offset": offset})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def keep_apart(client: TestClient, work_ids: list[str]) -> dict[str, Any]:
    response = client.post(KEEP, json={"work_ids": work_ids})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def undo(client: TestClient, first: str, second: str) -> None:
    response = client.post(UNDO, json={"first_work_id": first, "second_work_id": second})
    assert response.status_code == 204, response.text


def pair_json(first: str, second: str) -> dict[str, str]:
    a, b = ka.ordered(first, second)
    return {"first_work_id": a, "second_work_id": b}


def group_ids(body: dict[str, Any]) -> list[set[str]]:
    return [{g["target_id"], *g["source_ids"]} for g in body["groups"]]


def merge_count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM work_merges").fetchone()
    assert row is not None
    return int(row["n"])


def musicbrainz_work(conn: Conn, work_id: str) -> str:
    mbid = str(uuid4())
    conn.execute(
        "UPDATE works SET mbid = %s, origin = 'musicbrainz' WHERE id = %s", (mbid, work_id)
    )
    return mbid


def cached_lookup(conn: Conn, recording_mbid: str, work_mbid: str, title: str) -> None:
    payload = {
        "id": recording_mbid,
        "relations": [{"type": "performance", "work": {"id": work_mbid, "title": title}}],
    }
    conn.execute(
        """INSERT INTO mb_cache (cache_key, entity_type, entity_mbid, response_data, expires_at)
           VALUES (%s, 'recording', %s, %s, now() + interval '30 days')""",
        (f"recording:{recording_mbid}", recording_mbid, Jsonb(payload)),
    )


def credited_work(conn: Conn, title: str, artist_id: str, **performance: Any) -> str:
    work_id = cp.work(conn, title, artist_id)
    copy = cp.copy(conn, work_id, title=title)
    cp.performance(conn, work_id, [copy], credit=[artist_id], **performance)
    seed.master(conn, work_id, copy)
    return work_id


# --- the button (D55) -------------------------------------------------------------------------


def test_keep_apart_marks_every_pair_and_the_group_leaves(client: TestClient, ready: Conn) -> None:
    takes = ThreeTakes(ready)
    ready.commit()
    assert group_ids(page(client)) == [set(takes.works)]

    body = keep_apart(client, takes.works)

    every = sorted(
        [
            pair_json(takes.studio, takes.live),
            pair_json(takes.studio, takes.demo),
            pair_json(takes.live, takes.demo),
        ],
        key=lambda p: (p["first_work_id"], p["second_work_id"]),
    )
    assert body == {"kept": every}
    marks = ka.marks(ready)
    assert set(marks) == {(p["first_work_id"], p["second_work_id"]) for p in every}
    assert {m["note"] for m in marks.values()} == {ka.MERGE_LIST_NOTE}
    after = page(client)
    assert after["groups"] == [] and after["total"] == 0


def test_a_pair_already_kept_keeps_its_row(client: TestClient, ready: Conn) -> None:
    takes = ThreeTakes(ready)
    earlier = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    split = ka.keep(ready, takes.studio, takes.live, note=ka.split_note("always.mp3"), at=earlier)
    ready.commit()

    keep_apart(client, takes.works)
    keep_apart(client, takes.works)

    marks = ka.marks(ready)
    assert len(marks) == 3
    assert marks[split] == {"note": ka.split_note("always.mp3"), "kept_at": earlier}


def test_keep_apart_follows_a_merged_away_work(client: TestClient, ready: Conn) -> None:
    takes = ThreeTakes(ready)
    gone = f"work-{uuid4()}"
    ka.redirect(ready, gone, takes.demo)
    ready.commit()

    body = keep_apart(client, [gone, takes.studio])

    assert body == {"kept": [pair_json(takes.studio, takes.demo)]}
    assert ka.kept(ready) == {ka.ordered(takes.studio, takes.demo)}


@pytest.mark.parametrize(
    "which, status",
    [("one work", 422), ("the same work twice", 422), ("a work that is nowhere", 404)],
)
def test_keep_apart_refuses(client: TestClient, ready: Conn, which: str, status: int) -> None:
    takes = ThreeTakes(ready)
    ready.commit()
    work_ids = {
        "one work": [takes.studio],
        "the same work twice": [takes.studio, takes.studio],
        "a work that is nowhere": [takes.studio, f"work-{uuid4()}"],
    }[which]

    response = client.post(KEEP, json={"work_ids": work_ids})

    assert response.status_code == status, response.text
    assert ka.kept(ready) == set()


# --- Undo (D55, Q3) -------------------------------------------------------------------------


def test_undo_lets_the_pair_plan_again(client: TestClient, ready: Conn) -> None:
    world = TwoWorks(ready)
    first, second = ka.keep(ready, world.target, world.source, note=ka.MERGE_LIST_NOTE)
    ready.commit()
    assert page(client)["groups"] == []

    undo(client, second, first)

    assert ka.kept(ready) == set()
    body = page(client)
    assert group_ids(body) == [{world.target, world.source}]
    assert body["kept_apart"] == []


def test_undo_of_one_pair_of_three(client: TestClient, ready: Conn) -> None:
    """D55: Keep apart marked all three pairs; undoing studio–live lets just those two join."""
    takes = ThreeTakes(ready)
    ready.commit()
    keep_apart(client, takes.works)

    undo(client, takes.studio, takes.live)

    body = page(client)
    assert group_ids(body) == [{takes.studio, takes.live}]
    assert body["groups"][0]["held"] == []


def test_undo_of_a_pair_not_kept_is_204(client: TestClient, ready: Conn) -> None:
    world = TwoWorks(ready)
    kept = ka.keep(ready, world.target, ThreeTakes(ready).demo)
    ready.commit()

    undo(client, world.target, world.source)
    undo(client, world.target, f"work-{uuid4()}")

    assert ka.kept(ready) == {kept}


# --- the page's "Kept apart" rows (Q9, Q6) --------------------------------------------------


def test_the_kept_apart_section(client: TestClient, ready: Conn) -> None:
    """Every pair, newest first, each work named with its credited artists; the MusicBrainz line
    where a work link joins the pair; sent whole with every page."""
    labelle, aguilera = cp.artist(ready, "LaBelle"), cp.artist(ready, "Christina Aguilera")
    lab = credited_work(ready, "Lady Marmalade", labelle)
    marmalade = musicbrainz_work(ready, lab)
    agu = credited_work(ready, "Lady Marmalade", aguilera, recording_mbid="rec-agu-5c")
    cached_lookup(ready, "rec-agu-5c", marmalade, "Lady Marmalade")
    world = TwoWorks(ready)
    older = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
    newer = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    ka.keep(ready, lab, agu, note=ka.MERGE_LIST_NOTE, at=older)
    split = ka.keep(ready, world.target, world.source, note=ka.split_note("always.mp3"), at=newer)
    ready.commit()

    body = page(client)

    names = {lab: "LaBelle", agu: "Christina Aguilera", world.target: "Bon Jovi"}
    names[world.source] = "Bon Jovi"
    rows = body["kept_apart"]
    assert [(r["first"]["work_id"], r["second"]["work_id"]) for r in rows] == [
        split,
        ka.ordered(lab, agu),
    ]
    for row in rows:
        for side in ("first", "second"):
            work = row[side]
            assert set(work) == {"work_id", "title", "artists"}
            assert work["artists"] == [names[work["work_id"]]]
    assert [r["first"]["title"] for r in rows] == ["Always", "Lady Marmalade"]
    assert [r["note"] for r in rows] == [ka.split_note("always.mp3"), ka.MERGE_LIST_NOTE]
    assert [datetime.fromisoformat(r["kept_at"]) for r in rows] == [newer, older]
    assert [r["musicbrainz_links"] for r in rows] == [False, True]
    assert body["groups"] == []
    assert page(client, offset=50)["kept_apart"] == rows


# --- a held group (D56) -----------------------------------------------------------------------


def test_a_held_group_is_shown_whole_unpreviewed(client: TestClient, ready: Conn) -> None:
    """Studio and live are kept apart; demo still joins both, so the group stays, holding the
    pair. Unheld, its merge would change an AC line (live's override would reach it); held, it
    isn't previewed, and reading the page writes nothing."""
    takes = ThreeTakes(ready)
    cp.override(ready, takes.live, "AC", takes.live_copy)
    seed.heard(ready, takes.studio_copy, seed.station(ready, "AC"), title="Always", times=2)
    ready.commit()
    [unheld] = page(client)["groups"]
    assert unheld["changes"] != [] and unheld["held"] == []

    kept = ka.keep(ready, takes.studio, takes.live, note=ka.MERGE_LIST_NOTE)
    ready.commit()
    before, logged = seed.snapshot(ready), merge_count(ready)

    body = page(client)

    [held] = body["groups"]
    assert held["target_id"] == takes.studio
    assert held["source_ids"] == [takes.live, takes.demo]
    assert held["held"] == [pair_json(*kept)]
    assert held["changes"] == []
    assert body["total"] == 1
    assert seed.snapshot(ready) == before
    assert ka.kept(ready) == {kept}
    assert merge_count(ready) == logged


def test_apply_never_merges_a_held_group(client: TestClient, ready: Conn) -> None:
    takes = ThreeTakes(ready)
    ka.keep(ready, takes.studio, takes.live)
    ready.commit()
    [held] = page(client)["groups"]
    logged = merge_count(ready)

    response = client.post(
        APPLY,
        json={
            "groups": [
                {"target_id": held["target_id"], "source_ids": held["source_ids"], "approvals": []}
            ]
        },
    )

    assert response.status_code == 200, response.text
    assert [(r["target_id"], r["outcome"]) for r in response.json()["results"]] == [
        (takes.studio, "changed")
    ]
    assert all(seed.work_exists(ready, w) for w in takes.works)
    assert merge_count(ready) == logged


def test_a_held_group_keeps_its_place_among_the_others(client: TestClient, ready: Conn) -> None:
    """D37: groups come most played first, held or not."""
    takes = ThreeTakes(ready)
    ka.keep(ready, takes.studio, takes.live)
    louder = TwoWorks(ready, "Runaway")
    seed.heard(ready, louder.target_master, seed.station(ready), title="Runaway", times=20)
    ready.commit()

    body = page(client)

    assert [(g["target_id"], g["held"] != []) for g in body["groups"]] == [
        (louder.target, False),
        (takes.studio, True),
    ]
    assert body["total"] == 2


# --- the count (D58) --------------------------------------------------------------------------


def test_the_count_counts_kept_pairs(client: TestClient, ready: Conn) -> None:
    world = TwoWorks(ready)
    ka.keep(ready, world.target, world.source)
    ready.commit()

    response = client.get(COUNT)

    assert response.status_code == 200
    assert response.json() == {"total": 0, "duplicates": 0, "kept_works": 0, "kept_apart": 1}


# --- split and reassign through their routes (Q7) ---------------------------------------------


def test_the_split_route_marks_its_pair(client: TestClient, ready: Conn) -> None:
    world = TwoWorks(ready)
    name = ka.file_name(ready, world.source_copy)
    ready.commit()

    response = client.post(
        f"/api/v1/library/works/{world.source}/split", json={"file_id": str(world.source_copy)}
    )

    assert response.status_code == 201, response.text
    pair = ka.ordered(world.source, response.json()["new_work_id"])
    assert ka.marks(ready)[pair]["note"] == ka.split_note(name)


def test_the_reassign_route_marks_its_pair(client: TestClient, ready: Conn) -> None:
    world = TwoWorks(ready)
    name = ka.file_name(ready, world.source_copy)
    ready.commit()

    response = client.patch(
        f"/api/v1/library/files/{world.source_copy}/work", json={"work_id": world.target}
    )

    assert response.status_code == 200, response.text
    pair = ka.ordered(world.source, world.target)
    assert ka.marks(ready)[pair]["note"] == ka.reassign_note(name, "Always")
    assert page(client)["groups"] == []
