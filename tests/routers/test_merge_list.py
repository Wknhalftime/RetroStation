"""The merge list (artist model phase 3b PR 4; SPEC S1, S2, D6, D15, D26-D29, E3, E5, E9; scope
Q11-Q13, Q22; design C57, C58, C74; AUD-R080), on PostgreSQL.

``GET /api/v1/library/merge-list?offset=<n>&limit=<n>`` plans every group (the planner, from
the works' credits, cached work links and titles) and answers one page of them, most played
first (D37: ``offset`` 0 and ``limit`` 50 unless given, ``limit`` 1 to 50), stateless (Q13)::

    {"groups": [{"target_id", "source_ids", "joins" ("work_link", "shared_artist",
                 "shared_name"), "plays",
                 "works": [{"work_id", "title", "artists", "plays", "musicbrainz",
                            "master": {"file_id", "file_path", "method"} | null}, ...],
                 "flags": [{"kind", "work_id", "file_id", "identity_id", "text"}, ...],
                 "changes": [{"station_id", "station", "identity_id", "title", "artist",
                              "plays", "from_file_id", "from_path", "to_file_id",
                              "to_path"}, ...],
                 "failed": {"error", "logged_at"} | null}, ...],
     "duplicates": [{"recording_mbid", "works": [{"work_id", "title"}, ...]}, ...],
     "kept_works": [{"work_id", "title", "references"}, ...],
     "total": <every planned group>, "offset", "limit"}

A group's works list the target first, then its sources in order. Its ``flags`` only show
(Q11): ``masters_meet`` (a work's hand-picked master meets another work's master), ``cue_edit``
(a master's audio holds a manual cue edit), ``rejection`` (a line rejected a file of one of the
group's works, so 4.3 widens it to the merged work, E3). Its ``changes`` are what its guarded
merge would change in air play, a pin can't hold (C74): the merge of each group on the page
is tried in a savepoint that is always undone (D27, as D37 amends it), so reading the page
writes nothing, not even the merge log. ``failed`` is the latest failed attempt at the group,
from the merge log. The other two sections come whole with every page.

``GET /api/v1/library/merge-list/count`` answers ``{"total", "duplicates", "kept_works"}`` for
Library Status, from the planner alone (D27, D28).

``POST /api/v1/library/merge-list/apply`` takes ``{"groups": [{"target_id", "source_ids",
"approvals": [{"station_id", "identity_id", "file_id"}, ...]}, ...]}``: the groups of the page
Lance saw (D37), minus his skips, each with the changes he applied there (D6). It plans again,
and merges each
group whose works (merged-away ids followed) still sit in one planned group, as sent, through
the guard with reason "merge list", committing after each group (D26). It answers::

    {"results": [{"target_id", "source_ids", "outcome", "changes", "error"}, ...],
     "merged", "waiting", "failed", "already_merged", "changed"}

``merged``; ``waiting``: a change waits for Lance's flag, nothing merged, the group stays on
the list (Q12); ``failed``: the merge failed and was undone, its failed log row committed on
its own (D26); ``already_merged``: every source already redirects to the target (E5);
``changed``: its works no longer plan together, so nothing is merged unseen. A group a re-plan
added is never applied. Until D1's credits are done (its marker), all three routes answer 409
(Q22).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from backend.domain.merge_list import GroupOutcome, SeenGroup
from backend.domain.work_merges import MergeApproval
from backend.services import merge_list_service
from backend.services.repository_factory import RepositoryFactory, merge_list_repos
from backend.tasks.credit_pick_tasks import propose_credit_picks_task
from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.work_merge_seed import TwoWorks

pytestmark = pytest.mark.integration

PAGE = "/api/v1/library/merge-list"
COUNT = f"{PAGE}/count"
APPLY = f"{PAGE}/apply"
VARIOUS = "89ad4ac3-39f7-470e-963a-56509c546377"  # MusicBrainz's Various Artists
EDIT_HASH = "audio-sha256:" + "ab" * 32

type Conn = psycopg.Connection[dict]


@pytest.fixture
def ready(db_conn: Conn) -> Conn:
    """3a's build and D1's credits count as done (Q22)."""
    cp.mark_built(db_conn)
    cp.set_marker(db_conn, WORK_ARTISTS_CREDITED_SETTING)
    db_conn.commit()
    return db_conn


# --- calls ----------------------------------------------------------------------------------------


def page(client: TestClient) -> dict[str, Any]:
    response = client.get(PAGE)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def seen(group: dict[str, Any], *, approve: bool = False) -> dict[str, Any]:
    """What the page sends for a group Lance didn't skip: its works as shown and, with
    *approve*, every change it lists applied."""
    changes = group["changes"] if approve else []
    return {
        "target_id": group["target_id"],
        "source_ids": group["source_ids"],
        "approvals": [
            {
                "station_id": c["station_id"],
                "identity_id": c["identity_id"],
                "file_id": c["to_file_id"],
            }
            for c in changes
        ],
    }


def apply(client: TestClient, groups: list[dict[str, Any]]) -> dict[str, Any]:
    response = client.post(APPLY, json={"groups": groups})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def group_of(body: dict[str, Any], target: str) -> dict[str, Any]:
    [found] = [g for g in body["groups"] if g["target_id"] == target]
    return found


def outcomes(result: dict[str, Any]) -> list[tuple[str, str]]:
    return [(r["target_id"], r["outcome"]) for r in result["results"]]


def merge_count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM work_merges").fetchone()
    assert row is not None
    return int(row["n"])


# --- worlds ---------------------------------------------------------------------------------------


def same_song(conn: Conn, title: str, *, plays: int = 1) -> TwoWorks:
    """One act's song filed twice (TwoWorks), with a line on the target's master heard *plays*
    times, so the target is the planner's target (more matched plays)."""
    world = TwoWorks(conn, title)
    seed.heard(conn, world.target_master, seed.station(conn), title=title, times=plays)
    return world


def musicbrainz_work(conn: Conn, work_id: str) -> str:
    """Make *work_id* a MusicBrainz work; its work MBID."""
    mbid = str(uuid4())
    conn.execute(
        "UPDATE works SET mbid = %s, origin = 'musicbrainz' WHERE id = %s", (mbid, work_id)
    )
    return mbid


def cached_lookup(conn: Conn, recording_mbid: str, work_mbid: str, title: str) -> None:
    """A cached recording lookup whose performance relation names the work *work_mbid*."""
    payload = {
        "id": recording_mbid,
        "relations": [{"type": "performance", "work": {"id": work_mbid, "title": title}}],
    }
    conn.execute(
        """INSERT INTO mb_cache (cache_key, entity_type, entity_mbid, response_data, expires_at)
           VALUES (%s, 'recording', %s, %s, now() + interval '30 days')""",
        (f"recording:{recording_mbid}", recording_mbid, Jsonb(payload)),
    )


def credited_work(conn: Conn, title: str, artist_id: str, **performance: Any) -> tuple[str, UUID]:
    """A work of *artist_id* with one copy, its master, in a performance credited to it."""
    work_id = cp.work(conn, title, artist_id)
    copy = cp.copy(conn, work_id, title=title)
    cp.performance(conn, work_id, [copy], credit=[artist_id], **performance)
    seed.master(conn, work_id, copy)
    return work_id, copy


class Planned:
    """Three groups and one pair that never merges:

    - "Lady Marmalade": LaBelle's work is the MusicBrainz work, and Christina Aguilera's
      recording's cached lookup names it (``work_link``);
    - "School's Out": the band Alice Cooper and the singer Alice Cooper, two artists of one
      name (``shared_name``);
    - "Together Again": "Janet", merged into "Janet Jackson" by 2a (``shared_artist``);
    - "Poison" by Alice Cooper and by Bell Biv DeVoe: a title alone, never merged.
    """

    def __init__(self, conn: Conn) -> None:
        at = seed.station(conn)
        labelle, aguilera = cp.artist(conn, "LaBelle"), cp.artist(conn, "Christina Aguilera")
        self.labelle, labelle_copy = credited_work(conn, "Lady Marmalade", labelle)
        marmalade = musicbrainz_work(conn, self.labelle)
        self.aguilera, _ = credited_work(
            conn, "Lady Marmalade", aguilera, recording_mbid="rec-aguilera-marmalade"
        )
        cached_lookup(conn, "rec-aguilera-marmalade", marmalade, "Lady Marmalade")
        seed.heard(conn, labelle_copy, at, times=3)

        band, singer = cp.artist(conn, "Alice Cooper"), cp.artist(conn, "Alice Cooper")
        self.band, band_copy = credited_work(conn, "School's Out", band)
        self.singer, _ = credited_work(conn, "Schools Out", singer)
        seed.heard(conn, band_copy, at, title="School's Out", times=2)

        janet, jackson = cp.artist(conn, "Janet"), cp.artist(conn, "Janet Jackson")
        cp.merge(conn, janet, jackson)
        self.janet, _ = credited_work(conn, "Together Again", janet)
        self.jackson, jackson_copy = credited_work(conn, "Together Again", jackson)
        seed.heard(conn, jackson_copy, at, title="Together Again")

        bbd = cp.artist(conn, "Bell Biv DeVoe")
        self.poisons = [
            credited_work(conn, "Poison", band)[0],
            credited_work(conn, "Poison", bbd)[0],
        ]
        conn.commit()


# --- the page -------------------------------------------------------------------------------------


def test_lists_every_planned_group(client: TestClient, ready: Conn) -> None:
    world = Planned(ready)

    body = page(client)

    assert body["total"] == 3
    assert [(g["target_id"], g["source_ids"], g["joins"]) for g in body["groups"]] == [
        (world.labelle, [world.aguilera], ["work_link"]),
        (world.band, [world.singer], ["shared_name"]),
        (world.jackson, [world.janet], ["shared_artist"]),
    ]
    marmalade = group_of(body, world.labelle)
    assert marmalade["plays"] == 3
    assert [
        (w["work_id"], w["title"], w["artists"], w["plays"], w["musicbrainz"])
        for w in marmalade["works"]
    ] == [
        (world.labelle, "Lady Marmalade", ["LaBelle"], 3, True),
        (world.aguilera, "Lady Marmalade", ["Christina Aguilera"], 0, False),
    ]
    assert marmalade["works"][0]["master"]["method"] == "auto"
    assert all(g["flags"] == [] and g["changes"] == [] for g in body["groups"])
    assert all(g["failed"] is None for g in body["groups"])
    assert not {w for g in body["groups"] for w in [g["target_id"], *g["source_ids"]]} & set(
        world.poisons
    )


def test_reading_the_page_writes_nothing(client: TestClient, ready: Conn) -> None:
    """D27: each group's merge is tried in a savepoint and undone: no row of any table a merge
    writes changes, and the merge log gains nothing."""
    world = Planned(ready)
    before, logged = seed.snapshot(ready), merge_count(ready)

    page(client)

    assert seed.snapshot(ready) == before
    assert merge_count(ready) == logged
    assert seed.redirects(ready, [world.aguilera, world.singer, world.janet]) == {}


def test_flags_on_their_group(client: TestClient, ready: Conn) -> None:
    """Each group shows its own flags (S2): a hand-picked master meeting another master, a cue
    edit on a master's audio, and the C74 change a pin can't hold, by station and line (the
    source's AC override would reach the target's line at an AC station). A plain group shows
    none."""
    flagged = same_song(ready, "Always")
    ac_edit = flagged.source_file(ready)
    cp.override(ready, flagged.source, "AC", ac_edit)
    ac = seed.station(ready, "AC")
    ac_line = seed.heard(ready, flagged.target_master, ac, title="Always", times=2)
    picked = same_song(ready, "Bed Of Roses")
    seed.master(ready, picked.source, picked.source_master, "manual")
    edited = same_song(ready, "Runaway")
    ready.execute(
        "UPDATE library_files SET audio_hash = %s WHERE id = %s", (EDIT_HASH, edited.target_master)
    )
    ready.execute(
        """INSERT INTO stream_cue_edits (audio_hash, start_ms, eom_ms, fade_at_ms, stop_ms)
           VALUES (%s, 0, 190000, 195000, 200000)""",
        (EDIT_HASH,),
    )
    plain = same_song(ready, "Keep The Faith")
    ready.commit()

    body = page(client)

    [change] = group_of(body, flagged.target)["changes"]
    assert (change["station_id"], change["identity_id"]) == (str(ac), str(ac_line))
    assert (change["from_file_id"], change["to_file_id"]) == (
        str(flagged.target_master),
        str(ac_edit),
    )
    assert change["plays"] == 2
    assert group_of(body, flagged.target)["flags"] == []
    assert [(f["kind"], f["work_id"]) for f in group_of(body, picked.target)["flags"]] == [
        ("masters_meet", picked.source)
    ]
    assert [
        (f["kind"], f["work_id"], f["file_id"]) for f in group_of(body, edited.target)["flags"]
    ] == [("cue_edit", edited.target, str(edited.target_master))]
    assert group_of(body, picked.target)["changes"] == []
    assert group_of(body, plain.target)["flags"] == group_of(body, plain.target)["changes"] == []


def test_widened_rejection_flagged(client: TestClient, ready: Conn) -> None:
    """E3: a line rejected a file of the source; merged, 4.3 widens the rejection to the whole
    work (M26). The group is flagged with the line; in 3b it only shows (Q11)."""
    world = same_song(ready, "Livin' On A Prayer")
    rejecting = cp.line(ready, "Livin' On A Prayer", None)
    ready.execute(
        "UPDATE track_identities SET rejected_file_ids = %s WHERE id = %s",
        ([world.source_copy], rejecting),
    )
    ready.commit()

    flags = group_of(page(client), world.target)["flags"]

    assert [(f["kind"], f["identity_id"], f["work_id"]) for f in flags] == [
        ("rejection", str(rejecting), world.source)
    ]


def test_a_placeholder_credit_joins_nothing(client: TestClient, ready: Conn) -> None:
    """Two "Intro" works credited to Various Artists share a credit only through a placeholder,
    which is left out (as 3a's key leaves it out)."""
    various = cp.artist(ready, "Various Artists", mbid=VARIOUS)
    credited_work(ready, "Intro", various)
    credited_work(ready, "Intro", various)
    ready.commit()

    assert page(client)["groups"] == []


def test_the_kept_empty_works_section(client: TestClient, ready: Conn) -> None:
    """D15: the works the sweep keeps (no files, something pointing at them) are listed with
    what points at them."""
    world = same_song(ready, "Bad Medicine")
    kept = cp.work(ready, "Bad Medicine (old)", cp.artist(ready, "Somebody Else"))
    cp.override(ready, kept, "AC", world.target_master)
    ready.commit()

    body = page(client)

    assert [(k["work_id"], k["title"], k["references"]) for k in body["kept_works"]] == [
        (kept, "Bad Medicine (old)", ["format_override"])
    ]


def test_the_count(client: TestClient, ready: Conn) -> None:
    """D28: the planned groups, and the rows of the page's two other sections."""
    Planned(ready)
    elsewhere = cp.copy(ready, cp.work(ready, "Elsewhere", cp.artist(ready, "Nobody")))
    kept = cp.work(ready, "Emptied", cp.artist(ready, "Somebody"))
    cp.version_entry(ready, kept, "live", elsewhere)
    ready.commit()

    response = client.get(COUNT)

    assert response.status_code == 200
    assert response.json() == {"total": 3, "duplicates": 0, "kept_works": 1}


def test_empty(client: TestClient, ready: Conn) -> None:
    assert page(client) == {
        "groups": [],
        "duplicates": [],
        "kept_works": [],
        "total": 0,
        "offset": 0,
        "limit": 50,
    }
    assert client.get(COUNT).json() == {"total": 0, "duplicates": 0, "kept_works": 0}
    assert apply(client, []) == {
        "results": [],
        "merged": 0,
        "waiting": 0,
        "failed": 0,
        "already_merged": 0,
        "changed": 0,
    }


def pairs(conn: Conn, count: int) -> list[str]:
    """*count* songs, each filed twice by an act of its own (two credited works, one copy
    each, no plays); their targets, which the planner orders by the lower work id."""
    targets = []
    for n in range(count):
        act = cp.artist(conn, f"Act {n}")
        works = [credited_work(conn, f"Song {n}", act)[0] for _ in range(2)]
        targets.append(min(works))
    return sorted(targets)


def test_pages_of_fifty_most_played_first(client: TestClient, ready: Conn) -> None:
    """D37: 50 groups a page, most played first, ``total`` counting every planned group. A page
    past the end is empty; a limit outside 1 to 50, or a negative offset, is refused."""
    targets = pairs(ready, 51)
    loudest = same_song(ready, "Always", plays=3)
    ready.commit()

    first = client.get(PAGE).json()
    second = client.get(PAGE, params={"offset": 50}).json()

    assert (first["offset"], first["limit"], first["total"]) == (0, 50, 52)
    assert [g["target_id"] for g in first["groups"]] == [loudest.target, *targets[:49]]
    assert (second["offset"], second["limit"], second["total"]) == (50, 50, 52)
    assert [g["target_id"] for g in second["groups"]] == targets[49:]
    small = client.get(PAGE, params={"offset": 1, "limit": 2}).json()
    assert [g["target_id"] for g in small["groups"]] == targets[:2]
    assert client.get(PAGE, params={"offset": 60}).json()["groups"] == []
    for params in ({"limit": 51}, {"limit": 0}, {"offset": -1}):
        assert client.get(PAGE, params=params).status_code == 422


def test_it_waits_for_d1s_credits(client: TestClient, db_conn: Conn) -> None:
    """Q22: before D1's marker the planner can't trust the credits: every route answers 409."""
    cp.mark_built(db_conn)
    world = same_song(db_conn, "Always")
    db_conn.commit()
    body = {"groups": [{"target_id": world.target, "source_ids": [world.source], "approvals": []}]}

    for response in (client.get(PAGE), client.get(COUNT), client.post(APPLY, json=body)):
        assert response.status_code == 409
        assert "credits" in response.json()["detail"].lower()
    assert seed.work_exists(db_conn, world.source)


# --- Apply ----------------------------------------------------------------------------------------


def test_apply_sends_the_groups_seen_minus_skips(client: TestClient, ready: Conn) -> None:
    """Three groups; Lance skips the second. The first and third merge through the guard (each
    logged "merge list", its source redirecting); the skipped one stays on the list."""
    worlds = [
        same_song(ready, t, plays=3 - n) for n, t in enumerate(["Always", "Runaway", "Wanted"])
    ]
    ready.commit()
    shown = page(client)
    first, skipped, third = (group_of(shown, w.target) for w in worlds)

    result = apply(client, [seen(first), seen(third)])

    assert outcomes(result) == [(worlds[0].target, "merged"), (worlds[2].target, "merged")]
    assert (result["merged"], result["waiting"], result["failed"]) == (2, 0, 0)
    assert [seed.work_exists(ready, w.source) for w in worlds] == [False, True, False]
    assert seed.redirects(ready, [w.source for w in worlds]) == {
        worlds[0].source: worlds[0].target,
        worlds[2].source: worlds[2].target,
    }
    assert [m["reason"] for m in seed.merges_of(ready, worlds[0].target)] == ["merge list"]
    assert [g["target_id"] for g in page(client)["groups"]] == [skipped["target_id"]]


def test_a_group_a_re_plan_added_is_never_applied(client: TestClient, ready: Conn) -> None:
    """Between the page and Apply a new pair appears, and a third copy joins a shown group.
    Apply merges only the works Lance saw; both newcomers wait on the next page."""
    world = same_song(ready, "Always", plays=2)
    ready.commit()
    shown = group_of(page(client), world.target)
    newcomer = same_song(ready, "Runaway")
    third = cp.work(ready, "Always", world.act)
    third_copy = cp.copy(ready, third, fmt="mp3", title="Always")
    cp.performance(ready, third, [third_copy], credit=[world.act])
    ready.commit()

    result = apply(client, [seen(shown)])

    assert outcomes(result) == [(world.target, "merged")]
    assert not seed.work_exists(ready, world.source)
    assert seed.work_exists(ready, third) and seed.work_exists(ready, newcomer.source)
    after = page(client)
    assert group_of(after, world.target)["source_ids"] == [third]
    assert group_of(after, newcomer.target)["source_ids"] == [newcomer.source]


def test_a_changed_group_is_not_applied(client: TestClient, ready: Conn) -> None:
    """The source's credit moved to another act after the page was read: its works no longer
    plan together, so nothing merges unseen."""
    world = same_song(ready, "Always")
    ready.commit()
    shown = group_of(page(client), world.target)
    other = cp.artist(ready, "Somebody Else")
    ready.execute(
        """UPDATE performance_credits
              SET artist_id = %s, mbid = NULL, credited_name = 'Somebody Else'
            WHERE performance_id = %s""",
        (other, world.source_performance),
    )
    ready.commit()

    result = apply(client, [seen(shown)])

    assert outcomes(result) == [(world.target, "changed")]
    assert result["changed"] == 1
    assert seed.work_exists(ready, world.source)


def test_an_unapplied_flag_leaves_its_group(client: TestClient, ready: Conn) -> None:
    """Q12: a group whose change Lance didn't apply waits: nothing merged, its changes answered,
    the group still listed. Applied there (D6), the next Apply merges it with that change."""
    world = same_song(ready, "Always")
    ac_edit = world.source_file(ready)
    cp.override(ready, world.source, "AC", ac_edit)
    ac = seed.station(ready, "AC")
    ac_line = seed.heard(ready, world.target_master, ac, title="Always", times=2)
    ready.commit()
    shown = group_of(page(client), world.target)

    waiting = apply(client, [seen(shown)])

    [result] = waiting["results"]
    assert (result["outcome"], waiting["waiting"]) == ("waiting", 1)
    assert [c["identity_id"] for c in result["changes"]] == [str(ac_line)]
    assert seed.work_exists(ready, world.source)
    assert group_of(page(client), world.target)["changes"] == shown["changes"]

    merged = apply(client, [seen(shown, approve=True)])

    assert outcomes(merged) == [(world.target, "merged")]
    assert seed.playing(ready, ac_line) == {(ac_edit, "override")}


def test_a_failed_group_is_shown_failed(client: TestClient, ready: Conn) -> None:
    """The second group fails partway (a trigger planted by the test refuses deleting its
    source). On the page its preview fails, shown as ``failed`` with no log time (the preview
    logs nothing, D27). Apply undoes it alone and answers it failed with its error; the first
    stays merged. Its failed row is in the merge log, and the page then shows that row on the
    group, still planned."""
    first, second = same_song(ready, "Always", plays=2), same_song(ready, "Runaway")
    ready.execute(
        """CREATE FUNCTION refuse_one_work_delete() RETURNS trigger LANGUAGE plpgsql
           AS $$ BEGIN RAISE EXCEPTION 'work delete refused by the test'; END $$"""
    )
    ready.execute(
        sql.SQL(
            """CREATE TRIGGER refuse_one_work_delete BEFORE DELETE ON works FOR EACH ROW
               WHEN (OLD.id = {}) EXECUTE FUNCTION refuse_one_work_delete()"""
        ).format(sql.Literal(second.source))
    )
    ready.commit()
    try:
        shown = page(client)
        previewed = group_of(shown, second.target)["failed"]
        assert "work delete refused by the test" in previewed["error"]
        assert previewed["logged_at"] is None
        assert seed.merges_of(ready, second.target) == []

        result = apply(
            client, [seen(group_of(shown, first.target)), seen(group_of(shown, second.target))]
        )

        assert outcomes(result) == [(first.target, "merged"), (second.target, "failed")]
        assert "work delete refused by the test" in result["results"][1]["error"]
        assert not seed.work_exists(ready, first.source)
        assert seed.work_exists(ready, second.source)
        assert [m["status"] for m in seed.merges_of(ready, second.target)] == ["failed"]
        logged = group_of(page(client), second.target)["failed"]
        assert "work delete refused by the test" in logged["error"]
        assert logged["logged_at"] is not None
    finally:
        ready.rollback()
        ready.execute("DROP TRIGGER IF EXISTS refuse_one_work_delete ON works")
        ready.execute("DROP FUNCTION IF EXISTS refuse_one_work_delete()")
        ready.commit()


def test_apply_commits_after_each_group(ready: Conn) -> None:
    """D26: one merge per transaction. Apply commits after each group, the failed one too, so a
    failed group's row stays even when the caller then rolls back."""
    first, second = same_song(ready, "Always", plays=2), same_song(ready, "Runaway")
    ready.execute(
        """CREATE FUNCTION refuse_one_work_delete() RETURNS trigger LANGUAGE plpgsql
           AS $$ BEGIN RAISE EXCEPTION 'work delete refused by the test'; END $$"""
    )
    ready.execute(
        sql.SQL(
            """CREATE TRIGGER refuse_one_work_delete BEFORE DELETE ON works FOR EACH ROW
               WHEN (OLD.id = {}) EXECUTE FUNCTION refuse_one_work_delete()"""
        ).format(sql.Literal(second.source))
    )
    ready.commit()
    commits: list[str] = []

    def commit() -> None:
        commits.append("commit")
        ready.commit()

    try:
        groups = [
            SeenGroup(first.target, (first.source,), frozenset[MergeApproval]()),
            SeenGroup(second.target, (second.source,), frozenset[MergeApproval]()),
        ]
        report = merge_list_service.apply(
            groups, merge_list_repos(RepositoryFactory(ready)), commit
        )
        ready.rollback()

        assert [r.outcome for r in report.results] == [GroupOutcome.MERGED, GroupOutcome.FAILED]
        assert len(commits) == 2
        assert not seed.work_exists(ready, first.source)
        assert [m["status"] for m in seed.merges_of(ready, second.target)] == ["failed"]
    finally:
        ready.rollback()
        ready.execute("DROP TRIGGER IF EXISTS refuse_one_work_delete ON works")
        ready.execute("DROP FUNCTION IF EXISTS refuse_one_work_delete()")
        ready.commit()


def test_apply_twice(client: TestClient, ready: Conn) -> None:
    """E5: a second Apply of the same groups (or one merged meanwhile) changes nothing: its
    sources already redirect to its target."""
    world = same_song(ready, "Always")
    ready.commit()
    body = [seen(group_of(page(client), world.target))]
    apply(client, body)
    before, logged = seed.snapshot(ready), merge_count(ready)

    again = apply(client, body)

    assert outcomes(again) == [(world.target, "already_merged")]
    assert again["already_merged"] == 1
    assert seed.snapshot(ready) == before
    assert merge_count(ready) == logged
    assert seed.redirects(ready, [world.source]) == {world.source: world.target}


# --- E9: Credit picks after a merge ---------------------------------------------------------------


class Refiled(cp.Duet):
    """The duet world (its work the target: two duet lines on its master) and the same song
    filed again by Bon Jovi (the source): its own solo master and a duet copy in a duet
    performance of the same key as the target's. Two duet lines on the source's master were
    proposed the source's duet: one waits, one Lance skipped."""

    def __init__(self, conn: Conn) -> None:
        super().__init__(conn)
        self.duet_line(conn)
        self.duet_line(conn)
        title = "Who Says You Can't Go Home"
        self.source = cp.work(conn, title, self.bon_jovi)
        self.source_master = cp.copy(conn, self.source, title=title)
        cp.performance(conn, self.source, [self.source_master], credit=[self.bon_jovi])
        seed.master(conn, self.source, self.source_master)
        source_duet_copy = cp.copy(conn, self.source, fmt="mp3", title=title)
        self.source_duet = cp.performance(
            conn, self.source, [source_duet_copy], credit=[self.bon_jovi, self.nettles]
        )
        self.waiting_line = cp.line(conn, title, self.source_master, name=self.duet_name)
        cp.plays(conn, self.waiting_line)
        self.waiting = cp.propose(conn, self.waiting_line, self.source_duet)
        self.skipped_line = cp.line(conn, title, self.source_master, name=self.duet_name)
        self.skipped = cp.propose(conn, self.skipped_line, self.source_duet, state="skipped")
        conn.commit()


def test_proposals_after_merge(client: TestClient, ready: Conn) -> None:
    """E9: the merge folds the source's duet into the target's (same key), so the proposal that
    named it is stale: Credit picks' Apply skips it (3a S9). The proposals task, re-run after
    the merges, keeps the skip Lance made and proposes the merged work's duet for the line."""
    world = Refiled(ready)
    shown = group_of(page(client), world.work)
    assert shown["source_ids"] == [world.source]
    assert outcomes(apply(client, [seen(shown)])) == [(world.work, "merged")]

    picks = client.post(
        "/api/v1/library/credit-picks/apply", json={"apply": [str(world.waiting)], "skip": []}
    )
    assert picks.status_code == 200
    assert picks.json()["stale"] == [str(world.waiting)]

    assert propose_credit_picks_task.call_local()["done"] is True

    rows = {r["id"]: r for r in cp.proposals(ready)}
    assert rows[world.skipped]["state"] == "skipped"
    proposed = [
        r
        for r in rows.values()
        if r["identity_id"] == world.waiting_line and r["state"] == "proposed"
    ]
    assert [r["performance_id"] for r in proposed] == [world.duet]
