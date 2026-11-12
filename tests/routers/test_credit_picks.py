"""The Credit picks routes (performances phase 3a PR 3, SPEC S9, E6; design C57, C64, C65, C23;
AUD-R039), on PostgreSQL.

``GET /library/credit-picks`` answers two lists and a count:

- ``credit``: every undecided ``credit`` proposal, for Apply;
- ``version_lines``: every ``version_line`` and ``tie`` proposal, undecided or picked, for a
  per-line pick;
- ``total``: the undecided rows of both lists (also ``GET /library/credit-picks/count``, the
  Library Status count).

Each item carries its line (title, log name, plays), its work, what the line plays now (the
air-play view, at its latest play) and, for a ``credit`` row, what it would play: the
performance's preferred copy, else the copy Apply would make preferred (the master rules,
present before missing). A ``version_lines`` item lists its candidates, each with its credited
names and what it would play. Both lists are sorted by plays, most first, then by line id.
There is no paging: Apply's "ids he saw" is the whole list. A row that is stale when read (by
Apply's rule below) is left out of both lists and the count; a version line lists only its
candidates that are still non-default performances of its line's work, and with none left it is
stale.

``POST /library/credit-picks/apply`` takes ``apply`` (the ids Lance saw, minus his skips) and
``skip`` (his ticks). Applying stores the performance on the line's best match, through
``SongMatchService``, and marks the row ``applied``; a skip marks it ``skipped``. A row in neither
list (a re-run added it since) stays as it is. A stale row -- its line's best match changed, or
its performance is gone, on another work or now the default -- isn't applied, stays proposed,
and comes back in ``stale``; so does an id the page showed that a re-run has since replaced (a
changed answer is a new row, C64), so Apply never stores what Lance didn't see. Decided rows are
left alone, so pressing Apply twice changes nothing
(E6). Apply takes only ``credit`` rows (422), and an id in both lists is refused (422).

``PUT /library/credit-picks/{id}/pick`` takes ``performance_id``: one of a version line's
candidates, or null for none. It is stored at once (none stores no performance, so the version
entry plays), and the row is ``picked``; a pick can be changed. A candidate it doesn't list, or a
``credit`` row, is refused (422); an unknown id is 404; a stale row is 409.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from backend.tasks.credit_pick_tasks import propose_credit_picks_task
from tests.integration import credit_picks_seed as seed
from tests.integration.credit_picks_seed import Duet

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from tests.integration.credit_picks_seed import Conn

pytestmark = pytest.mark.integration

PAGE = "/api/v1/library/credit-picks"
COUNT = "/api/v1/library/credit-picks/count"
APPLY = "/api/v1/library/credit-picks/apply"


def pick_url(proposal_id: UUID) -> str:
    return f"/api/v1/library/credit-picks/{proposal_id}/pick"


def page(client: TestClient) -> dict[str, Any]:
    response = client.get(PAGE)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def apply(client: TestClient, applied: list[UUID], skipped: list[UUID]) -> dict[str, Any]:
    response = client.post(
        APPLY, json={"apply": [str(i) for i in applied], "skip": [str(i) for i in skipped]}
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def ids(items: list[dict[str, Any]]) -> list[UUID]:
    return [UUID(i["id"]) for i in items]


def state_of(conn: Conn, proposal_id: UUID) -> tuple[str, Any]:
    row = conn.execute(
        "SELECT state, decided_at FROM credit_pick_proposals WHERE id = %s", (proposal_id,)
    ).fetchone()
    assert row is not None
    return row["state"], row["decided_at"]


class World(Duet):
    """The duet world with a preferred duet copy, two duet lines proposed for Apply, a solo line
    and a live duet line listed apart (a version line), all on the solo master."""

    def __init__(self, conn: Conn) -> None:
        super().__init__(conn)
        conn.execute(
            "UPDATE performances SET preferred_copy_id = %s WHERE id = %s",
            (self.duet_flac, self.duet),
        )
        self.first = self.duet_line(conn)
        seed.plays(conn, self.first, 2)
        self.second = self.duet_line(conn)
        self.solo_line(conn)
        self.live_copy = seed.copy(conn, self.work, title="Who Says You Can't Go Home (Live)")
        self.live = seed.performance(
            conn,
            self.work,
            [self.live_copy],
            credit=[self.bon_jovi, self.nettles],
            recording_mbid="r-live-duet",
        )
        self.live_line = self.duet_line(conn, "Who Says You Can't Go Home (Live)")
        self.first_id = seed.propose(conn, self.first, self.duet)
        self.second_id = seed.propose(conn, self.second, self.duet)
        self.live_id = seed.propose(
            conn,
            self.live_line,
            None,
            kind="version_line",
            candidates=[self.duet, self.live],
        )


@pytest.fixture
def world(db_conn: Conn) -> World:
    built = World(db_conn)
    db_conn.commit()
    return built


# --- the lists ------------------------------------------------------------------------------------


def test_lists_the_proposals(client: TestClient, world: World) -> None:
    body = page(client)

    assert body["total"] == 3
    assert ids(body["credit"]) == [world.first_id, world.second_id]  # by plays, most first
    first = body["credit"][0]
    assert {k: first[k] for k in ("kind", "reason", "state", "title", "artist_name", "plays")} == {
        "kind": "credit",
        "reason": "credit",
        "state": "proposed",
        "title": "Who Says You Can't Go Home",
        "artist_name": "BON JOVI/JENNIFER NETTLES",
        "plays": 3,
    }
    assert UUID(first["identity_id"]) == world.first
    assert (first["work_id"], first["work_title"]) == (world.work, "Who Says You Can't Go Home")
    assert UUID(first["performance_id"]) == world.duet
    assert (UUID(first["plays_now"]["file_id"]), first["plays_now"]["source"]) == (
        world.master,
        "master",
    )
    assert UUID(first["would_play"]["file_id"]) == world.duet_flac


def test_lists_version_lines_with_their_candidates(client: TestClient, world: World) -> None:
    [item] = page(client)["version_lines"]

    assert UUID(item["id"]) == world.live_id
    assert (item["kind"], item["state"], item["performance_id"]) == (
        "version_line",
        "proposed",
        None,
    )
    assert item["title"] == "Who Says You Can't Go Home (Live)"
    candidates = {UUID(c["performance_id"]): c for c in item["candidates"]}
    assert set(candidates) == {world.duet, world.live}
    assert candidates[world.duet]["credit_names"] == ["Bon Jovi", "Jennifer Nettles"]
    assert UUID(candidates[world.duet]["would_play"]["file_id"]) == world.duet_flac
    assert UUID(candidates[world.live]["would_play"]["file_id"]) == world.live_copy


def test_would_play_the_copy_apply_would_prefer(client: TestClient, db_conn: Conn) -> None:
    """With no preferred copy yet, the flac copy over the mp3 one; present before missing."""
    duet = Duet(db_conn)
    seed.propose(db_conn, duet.duet_line(db_conn), duet.duet)
    db_conn.execute(
        "UPDATE library_files SET file_status = 'missing' WHERE id = %s", (duet.duet_mp3,)
    )
    db_conn.commit()

    [item] = page(client)["credit"]
    assert UUID(item["would_play"]["file_id"]) == duet.duet_flac

    db_conn.execute(
        "UPDATE library_files SET file_status = 'missing' WHERE id = %s", (duet.duet_flac,)
    )
    db_conn.execute(
        "UPDATE library_files SET file_status = 'present' WHERE id = %s", (duet.duet_mp3,)
    )
    db_conn.commit()

    [item] = page(client)["credit"]
    assert UUID(item["would_play"]["file_id"]) == duet.duet_mp3


def test_decided_rows_leave_the_lists_but_picked_ones_stay(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    db_conn.execute(
        "UPDATE credit_pick_proposals SET state = 'skipped', decided_at = now() WHERE id = %s",
        (world.first_id,),
    )
    db_conn.execute(
        "UPDATE credit_pick_proposals SET state = 'picked', decided_at = now() WHERE id = %s",
        (world.live_id,),
    )
    db_conn.commit()

    body = page(client)

    assert ids(body["credit"]) == [world.second_id]
    assert [(UUID(i["id"]), i["state"]) for i in body["version_lines"]] == [
        (world.live_id, "picked")
    ]
    assert body["total"] == 1


def test_empty(client: TestClient) -> None:
    assert page(client) == {"credit": [], "version_lines": [], "total": 0}
    assert client.get(COUNT).json() == {"total": 0}


def test_the_count(client: TestClient, world: World) -> None:
    response = client.get(COUNT)

    assert response.status_code == 200
    assert response.json() == {"total": 3}


def test_a_stale_row_is_left_out_of_the_lists_and_the_count(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    """The first line has a better match now, a third row's performance is gone and the live
    line was re-resolved: only the second row is listed and counted."""
    seed.also_match(db_conn, world.first, world.duet_mp3, confidence=0.99)
    seed.also_match(db_conn, world.live_line, world.duet_mp3, confidence=0.99)
    third = world.duet_line(db_conn)
    gone_copy = seed.copy(db_conn, world.work)
    gone = seed.performance(
        db_conn, world.work, [gone_copy], credit=[world.bon_jovi, world.nettles], recording_mbid="g"
    )
    seed.propose(db_conn, third, gone)
    db_conn.execute("UPDATE library_files SET performance_id = NULL WHERE id = %s", (gone_copy,))
    db_conn.execute("DELETE FROM performances WHERE id = %s", (gone,))
    db_conn.commit()

    body = page(client)

    assert ids(body["credit"]) == [world.second_id]
    assert body["version_lines"] == []
    assert body["total"] == 1
    assert client.get(COUNT).json() == {"total": 1}


def test_a_row_whose_performance_became_the_default_is_left_out(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    """Set master moved the master into the duet: the duet rows are stale; the live line keeps
    its live candidate only."""
    db_conn.execute(
        "UPDATE song_masters SET preferred_file_id = %s WHERE work_id = %s",
        (world.duet_mp3, world.work),
    )
    db_conn.commit()

    body = page(client)

    assert body["credit"] == []
    [live] = body["version_lines"]
    assert [UUID(c["performance_id"]) for c in live["candidates"]] == [world.live]
    assert body["total"] == 1


# --- Apply ----------------------------------------------------------------------------------------


def test_apply_sends_seen_ids(client: TestClient, db_conn: Conn, world: World) -> None:
    """C64, AUD-R039: what Lance saw minus his skips is applied, his skips are recorded, and a
    row added after he loaded the page is neither."""
    seen = ids(page(client)["credit"])
    later = seed.line(db_conn, "Who Says You Can't Go Home", world.master, name=world.duet_name)
    later_id = seed.propose(db_conn, later, world.duet)
    db_conn.commit()

    result = apply(client, [i for i in seen if i != world.second_id], [world.second_id])

    assert result == {"applied": 1, "skipped": 1, "stale": []}
    assert state_of(db_conn, world.first_id)[0] == "applied"
    assert state_of(db_conn, world.second_id)[0] == "skipped"
    assert state_of(db_conn, later_id) == ("proposed", None)
    assert seed.stored(db_conn, world.first) == {world.duet}
    assert seed.stored(db_conn, world.second) == {None}
    assert seed.stored(db_conn, later) == {None}
    assert seed.resolved(db_conn, world.first) == {(world.duet_flac, "performance")}
    assert seed.resolved(db_conn, world.second) == {(world.master, "master")}


def test_apply_never_stores_an_answer_lance_did_not_see(client: TestClient, db_conn: Conn) -> None:
    """C64, AUD-R039: the page shows the duet for "JENNIFER NETTLES"; a re-run then finds her own
    performance, a new answer, so a new row. Applying the row he saw stores neither."""
    duet = Duet(db_conn)
    seed.mark_built(db_conn)
    line = seed.line(
        db_conn,
        "Who Says You Can't Go Home",
        duet.master,
        name=seed.log_name(db_conn, "JENNIFER NETTLES", duet.nettles),
    )
    db_conn.commit()
    propose_credit_picks_task.call_local()
    [seen] = page(client)["credit"]
    her_copy = seed.copy(db_conn, duet.work)
    hers = seed.performance(
        db_conn, duet.work, [her_copy], credit=[duet.nettles], recording_mbid="n1"
    )
    db_conn.commit()
    propose_credit_picks_task.call_local()

    result = apply(client, [UUID(seen["id"])], [])

    assert UUID(seen["performance_id"]) == duet.duet
    assert result == {"applied": 0, "skipped": 0, "stale": [seen["id"]]}
    assert seed.stored(db_conn, line) == {None}
    [now] = page(client)["credit"]
    assert UUID(now["performance_id"]) == hers and now["id"] != seen["id"]


def test_apply_stores_on_the_best_match_only(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    weaker = seed.also_match(db_conn, world.first, world.duet_mp3, confidence=0.1)
    db_conn.commit()

    apply(client, [world.first_id], [])

    row = db_conn.execute("SELECT performance_id FROM matches WHERE id = %s", (weaker,)).fetchone()
    assert row is not None and row["performance_id"] is None
    assert seed.stored(db_conn, world.first) == {world.duet, None}


def test_apply_twice(client: TestClient, db_conn: Conn, world: World) -> None:
    """E6: the second press changes nothing."""
    first = apply(client, [world.first_id], [world.second_id])
    states = [state_of(db_conn, i) for i in (world.first_id, world.second_id)]

    second = apply(client, [world.first_id], [world.second_id])

    assert first == {"applied": 1, "skipped": 1, "stale": []}
    assert second == {"applied": 0, "skipped": 0, "stale": []}
    assert [state_of(db_conn, i) for i in (world.first_id, world.second_id)] == states
    assert seed.stored(db_conn, world.first) == {world.duet}


def test_a_skip_after_apply_leaves_it_applied(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    apply(client, [world.first_id], [])

    result = apply(client, [], [world.first_id])

    assert result == {"applied": 0, "skipped": 0, "stale": []}
    assert state_of(db_conn, world.first_id)[0] == "applied"
    assert seed.stored(db_conn, world.first) == {world.duet}


def test_a_stale_proposal_is_skipped_and_reported(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    """The first line was re-resolved (a better match now); the duet became the default for the
    second; a third proposal's performance is gone. None is applied; each stays proposed."""
    seed.also_match(db_conn, world.first, world.duet_mp3, confidence=0.99)
    third = world.duet_line(db_conn)
    gone_copy = seed.copy(db_conn, world.work)
    gone = seed.performance(
        db_conn, world.work, [gone_copy], credit=[world.bon_jovi, world.nettles], recording_mbid="x"
    )
    third_id = seed.propose(db_conn, third, gone)
    db_conn.execute("UPDATE library_files SET performance_id = NULL WHERE id = %s", (gone_copy,))
    db_conn.execute("DELETE FROM performances WHERE id = %s", (gone,))
    db_conn.execute(
        "UPDATE song_masters SET preferred_file_id = %s WHERE work_id = %s",
        (world.duet_mp3, world.work),
    )
    db_conn.commit()

    result = apply(client, [world.first_id, world.second_id, third_id], [])

    assert result["applied"] == 0 and result["skipped"] == 0
    assert sorted(UUID(i) for i in result["stale"]) == sorted(
        [world.first_id, world.second_id, third_id]
    )
    for proposal_id in (world.first_id, world.second_id, third_id):
        assert state_of(db_conn, proposal_id) == ("proposed", None)
    assert seed.stored(db_conn, world.first) == {None}
    assert seed.stored(db_conn, world.second) == {None}


def test_an_unknown_id_is_reported_stale(client: TestClient, world: World) -> None:
    """A row a re-run dropped since the page loaded."""
    unknown = uuid4()

    result = apply(client, [unknown], [])

    assert result == {"applied": 0, "skipped": 0, "stale": [str(unknown)]}


def test_apply_takes_only_credit_rows(client: TestClient, db_conn: Conn, world: World) -> None:
    applying = client.post(APPLY, json={"apply": [str(world.live_id)], "skip": []})
    skipping = client.post(APPLY, json={"apply": [], "skip": [str(world.live_id)]})
    both = client.post(APPLY, json={"apply": [str(world.first_id)], "skip": [str(world.first_id)]})

    assert (applying.status_code, skipping.status_code, both.status_code) == (422, 422, 422)
    assert state_of(db_conn, world.live_id) == ("proposed", None)
    assert state_of(db_conn, world.first_id) == ("proposed", None)
    assert seed.stored(db_conn, world.first) == {None}


# --- the version-line pick ------------------------------------------------------------------------


def test_version_line_pick(client: TestClient, db_conn: Conn, world: World) -> None:
    """A pick is stored at once and plays before the line's version entry (C61); it can be
    changed; none stores no performance, so the entry plays."""
    entry_copy = seed.copy(db_conn, world.work)
    seed.performance(db_conn, world.work, [entry_copy], credit=[world.bon_jovi], recording_mbid="e")
    seed.version_entry(db_conn, world.work, "live", entry_copy)
    db_conn.commit()

    live = client.put(pick_url(world.live_id), json={"performance_id": str(world.live)})
    after_live = seed.resolved(db_conn, world.live_line)
    changed = client.put(pick_url(world.live_id), json={"performance_id": str(world.duet)})
    after_duet = seed.resolved(db_conn, world.live_line)
    none = client.put(pick_url(world.live_id), json={"performance_id": None})

    assert (live.status_code, changed.status_code, none.status_code) == (200, 200, 200)
    assert after_live == {(world.live_copy, "performance")}
    assert after_duet == {(world.duet_flac, "performance")}
    assert seed.resolved(db_conn, world.live_line) == {(entry_copy, "version")}
    assert seed.stored(db_conn, world.live_line) == {None}
    state, decided_at = state_of(db_conn, world.live_id)
    assert state == "picked" and decided_at is not None
    row = seed.proposal_of(db_conn, world.live_line)
    assert row is not None and row["performance_id"] is None


def test_a_pick_gives_its_performance_a_preferred_copy(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    assert seed.preferred_of(db_conn, world.live) is None

    client.put(pick_url(world.live_id), json={"performance_id": str(world.live)})

    assert seed.preferred_of(db_conn, world.live) == world.live_copy


def test_a_tie_is_picked_the_same_way(client: TestClient, db_conn: Conn, world: World) -> None:
    tie_line = world.duet_line(db_conn)
    tie_id = seed.propose(db_conn, tie_line, None, kind="tie", candidates=[world.duet, world.live])
    db_conn.commit()

    response = client.put(pick_url(tie_id), json={"performance_id": str(world.live)})

    assert response.status_code == 200
    assert seed.stored(db_conn, tie_line) == {world.live}


def test_a_pick_refuses_what_it_does_not_list(
    client: TestClient, db_conn: Conn, world: World
) -> None:
    not_listed = client.put(pick_url(world.live_id), json={"performance_id": str(world.solo)})
    on_credit_row = client.put(pick_url(world.first_id), json={"performance_id": str(world.duet)})
    unknown = client.put(pick_url(uuid4()), json={"performance_id": str(world.duet)})

    assert (not_listed.status_code, on_credit_row.status_code, unknown.status_code) == (
        422,
        422,
        404,
    )
    assert state_of(db_conn, world.live_id) == ("proposed", None)
    assert seed.stored(db_conn, world.live_line) == {None}


def test_a_stale_pick_is_refused(client: TestClient, db_conn: Conn, world: World) -> None:
    """The live duet became the default: picking it would store the default (C71)."""
    db_conn.execute(
        "UPDATE song_masters SET preferred_file_id = %s WHERE work_id = %s",
        (world.live_copy, world.work),
    )
    db_conn.commit()

    response = client.put(pick_url(world.live_id), json={"performance_id": str(world.live)})

    assert response.status_code == 409
    assert state_of(db_conn, world.live_id) == ("proposed", None)
    assert seed.stored(db_conn, world.live_line) == {None}
