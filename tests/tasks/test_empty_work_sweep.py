"""The empty-work sweep and one deletion rule (artist model phase 3b PR 4; SPEC S6, D7, D15, D16;
scope Q16, Q17; design C53, §6.9; AUD-R045, AUD-R080), on PostgreSQL.

A work with no files is deleted only when nothing points at it: ``delete_if_empty``'s check,
the one rule. Its song master goes with it, as today. What keeps an empty work:

- ``recording``: a recording of the work;
- ``match``: a match carrying the work (``matches.work_id``);
- ``work_match``: a match whose target is the work;
- ``format_override`` and ``version_entry``: an override row of either kind;
- ``stored_performance``: a performance of the work that a match stores (Q17: 3a deletes an
  emptied performance, so only a direct seed makes one).

The sweep (``sweep_empty_works_task``, run by name after the merges and again after phase 4's)
deletes every empty work nothing points at and keeps the rest, which
``WorkRepository.list_empty_works`` lists for Lance with what points at each (the merge list's
section, D15), by title. It has no marker: a second run finds only what emptied since.

Split and reassign use the same rule (D7): a work they empty first loses its file-less
recordings (D16, as today), then stays while anything else points at it; it loses only a master
left on a file of another work.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.domain.catalog import EmptyWork, Recording, WorkReference
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.work_curation_service import reassign_file, split_file_off
from backend.tasks import huey_app
from backend.tasks.empty_work_sweep_tasks import sweep_empty_works_task
from tests.integration import credit_picks_seed as cp

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """The test's connection; the task's own connection uses the test database, and nothing it
    might queue leaves the test."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setattr(huey_app.huey, "enqueue", lambda _task: None)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection
    get_settings.cache_clear()


def sweep(conn: Conn) -> dict[str, Any]:
    """One run of the sweep, with the test's work committed first."""
    conn.commit()
    result: dict[str, Any] = sweep_empty_works_task.call_local()
    conn.rollback()
    return result


# --- seeds and reads ------------------------------------------------------------------------------


def work_exists(conn: Conn, work_id: str) -> bool:
    return conn.execute("SELECT 1 FROM works WHERE id = %s", (work_id,)).fetchone() is not None


def master_of(conn: Conn, work_id: str) -> UUID | None:
    row = conn.execute(
        "SELECT preferred_file_id FROM song_masters WHERE work_id = %s", (work_id,)
    ).fetchone()
    return None if row is None else UUID(str(row["preferred_file_id"]))


def recording(conn: Conn, work_id: str) -> str:
    return (
        RepositoryFactory(conn)
        .recordings.upsert(
            Recording(id=str(uuid4()), title="take", work_id=work_id, needs_enhancement=False)
        )
        .id
    )


def recording_exists(conn: Conn, recording_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM recordings WHERE id = %s", (recording_id,)).fetchone()
    return row is not None


def elsewhere(conn: Conn) -> UUID:
    """A file on a work of its own, for references to point at."""
    return cp.copy(conn, cp.work(conn, "Elsewhere", cp.artist(conn, "Somebody")))


def empty_work(conn: Conn, title: str = "Lithium") -> str:
    return cp.work(conn, title, cp.artist(conn, "Nirvana"))


def refer(conn: Conn, work_id: str, kind: WorkReference) -> None:
    """Point one thing of *kind* at the empty work *work_id*."""
    if kind is WorkReference.RECORDING:
        recording(conn, work_id)
    elif kind is WorkReference.MATCH:
        line = cp.line(conn, "Lithium", None)
        conn.execute(
            """INSERT INTO matches (identity_id, library_file_id, target_type, work_id,
                                    confidence_score)
               VALUES (%s, %s, 'library_file', %s, 0.9)""",
            (line, elsewhere(conn), work_id),
        )
    elif kind is WorkReference.WORK_MATCH:
        line = cp.line(conn, "Lithium", None)
        conn.execute(
            """INSERT INTO matches (identity_id, target_type, target_id, confidence_score)
               VALUES (%s, 'work', %s, 0.5)""",
            (line, work_id),
        )
    elif kind is WorkReference.FORMAT_OVERRIDE:
        cp.override(conn, work_id, "AC", elsewhere(conn))
    elif kind is WorkReference.VERSION_ENTRY:
        cp.version_entry(conn, work_id, "live", elsewhere(conn))
    elif kind is WorkReference.STORED_PERFORMANCE:
        held = cp.performance(conn, work_id, [])
        cp.store(conn, cp.line(conn, "Lithium", elsewhere(conn)), held)


def listed(conn: Conn) -> list[EmptyWork]:
    return RepositoryFactory(conn).works.list_empty_works()


# --- the sweep ------------------------------------------------------------------------------------


def test_deletes_unreferenced_empty_works_with_their_master(conn: Conn) -> None:
    """Two empty works nothing points at go, one with its master (on a file of another work)
    and its version states; a work with a file is left alone."""
    mastered, bare = empty_work(conn, "Lithium"), empty_work(conn, "Breed")
    cp.master(conn, mastered, elsewhere(conn))
    conn.execute(
        """INSERT INTO version_entry_states (work_id, version_text, state, seen_at)
           VALUES (%s, 'live', 'dismissed', now())""",
        (mastered,),
    )
    full = cp.work(conn, "In Bloom", cp.artist(conn, "Nirvana"))
    cp.copy(conn, full)

    assert sweep(conn) == {"deleted": 2, "kept": 0}

    assert not work_exists(conn, mastered) and not work_exists(conn, bare)
    assert master_of(conn, mastered) is None
    assert work_exists(conn, full)
    assert listed(conn) == []


@pytest.mark.parametrize("kind", list(WorkReference))
def test_keeps_and_lists_each_kind_of_reference(conn: Conn, kind: WorkReference) -> None:
    kept = empty_work(conn)
    refer(conn, kept, kind)

    assert sweep(conn) == {"deleted": 0, "kept": 1}

    assert work_exists(conn, kept)
    assert listed(conn) == [EmptyWork(work_id=kept, title="Lithium", references=(kind,))]


def test_every_reference_of_a_work_is_listed_in_order(conn: Conn) -> None:
    """The list names each kind once, in the order above, and the works by title."""
    first, second = empty_work(conn, "Breed"), empty_work(conn, "Aneurysm")
    refer(conn, first, WorkReference.VERSION_ENTRY)
    refer(conn, first, WorkReference.FORMAT_OVERRIDE)
    cp.override(conn, first, "CHR", elsewhere(conn))
    refer(conn, second, WorkReference.RECORDING)
    conn.commit()

    assert listed(conn) == [
        EmptyWork(second, "Aneurysm", (WorkReference.RECORDING,)),
        EmptyWork(first, "Breed", (WorkReference.FORMAT_OVERRIDE, WorkReference.VERSION_ENTRY)),
    ]


def test_it_runs_again_idempotently(conn: Conn) -> None:
    """No marker: a second run keeps what it kept and deletes only what emptied since."""
    kept = empty_work(conn, "Lithium")
    refer(conn, kept, WorkReference.FORMAT_OVERRIDE)
    empty_work(conn, "Breed")
    assert sweep(conn) == {"deleted": 1, "kept": 1}

    assert sweep(conn) == {"deleted": 0, "kept": 1}

    later = empty_work(conn, "Polly")
    assert sweep(conn) == {"deleted": 1, "kept": 1}
    assert not work_exists(conn, later)
    assert [w.work_id for w in listed(conn)] == [kept]


# --- D7: split and reassign use the rule ----------------------------------------------------------


def test_split_keeps_an_emptied_work_an_override_names(conn: Conn) -> None:
    """D7, D16: the split takes the work's last file; its recording (now file-less) goes, the
    master on the file that left goes, and the override keeps the work, listed for Lance."""
    work = empty_work(conn)
    take = recording(conn, work)
    only = cp.copy(conn, work)
    conn.execute("UPDATE library_files SET recording_id = %s WHERE id = %s", (take, only))
    cp.override(conn, work, "AC", only)
    cp.master(conn, work, only)

    result = split_file_off(work, only, work_curation_repos(RepositoryFactory(conn)))

    assert result.old_work_deleted is False
    assert work_exists(conn, work)
    assert not recording_exists(conn, take)
    assert master_of(conn, work) is None
    assert listed(conn) == [EmptyWork(work, "Lithium", (WorkReference.FORMAT_OVERRIDE,))]


def test_reassign_keeps_an_emptied_work_a_match_names(conn: Conn) -> None:
    """D7: the reassign takes the work's last file; a line's match on the work itself keeps it."""
    work = empty_work(conn)
    only = cp.copy(conn, work)
    cp.master(conn, work, only)
    refer(conn, work, WorkReference.WORK_MATCH)
    target = cp.work(conn, "Lithium", cp.artist(conn, "Nirvana"))
    cp.copy(conn, target)

    result = reassign_file(only, target, work_curation_repos(RepositoryFactory(conn)))

    assert result.old_work_deleted is False
    assert work_exists(conn, work)
    assert master_of(conn, work) is None
    assert listed(conn) == [EmptyWork(work, "Lithium", (WorkReference.WORK_MATCH,))]


def test_split_deletes_an_emptied_work_nothing_names(conn: Conn) -> None:
    """D16: with only its file-less recording and its master left, the emptied work goes with
    both, as today."""
    work = empty_work(conn)
    take = recording(conn, work)
    only = cp.copy(conn, work)
    conn.execute("UPDATE library_files SET recording_id = %s WHERE id = %s", (take, only))
    cp.master(conn, work, only)

    result = split_file_off(work, only, work_curation_repos(RepositoryFactory(conn)))

    assert result.old_work_deleted is True
    assert not work_exists(conn, work)
    assert not recording_exists(conn, take)
    assert master_of(conn, work) is None
    assert listed(conn) == []
