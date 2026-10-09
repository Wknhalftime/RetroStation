"""Acceptance tests: link_local_artists_task (AUD-R026; spec 2026-10-05 D15).

The task runs inside task_run as TaskType.ARTIST_LINKING. It lists the due local artists, then
decides and writes each one in its own transaction, and reports a count per outcome. Any HTTP
failure (a 429/5xx after retries, a 403 from a proxy, a network drop) or a database error rolls
that artist back and leaves it due. After 10 failed artists in a row the run stops early (an
outage): the rest stay due, and the run still completes. The task hands nothing off.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import uuid4

import httpx
import psycopg
import pytest

from backend.domain.catalog import Artist, LinkEvidence
from backend.domain.enums import ArtistLinkOutcome, TaskStatus, TaskType
from backend.domain.system import TaskProgress
from backend.services.normalization import normalize_artist
from tests.fakes.artist_linking import FakeArtistLinkingRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

REPO = Path(__file__).resolve().parents[2]


def _mbid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


NIRVANA = _mbid(1)
SOUNDGARDEN = _mbid(2)
OZZY_OSBOURNE = _mbid(3)
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
NO_LINK = {outcome.value: 0 for outcome in ArtistLinkOutcome}


class _Mb(FakeMbClient):
    """FakeMbClient as a context manager whose lookups can fail on demand."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.refuse: dict[str, int] = {}  # MBID -> HTTP status
        self.network_down: set[str] = set()
        self.asked: list[str] = []  # every lookup, refused or not

    def __enter__(self) -> _Mb:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def lookup_artist(self, mbid: str) -> dict[str, Any] | None:
        self.asked.append(mbid)
        if mbid in self.network_down:
            raise httpx.ConnectError("simulated network drop")
        status = self.refuse.get(mbid)
        if status is not None:
            request = httpx.Request("GET", f"https://musicbrainz.org/ws/2/artist/{mbid}")
            response = httpx.Response(status, request=request)
            raise httpx.HTTPStatusError(str(status), request=request, response=response)
        return super().lookup_artist(mbid)


class _Repo(FakeArtistLinkingRepository):
    """record_outcome fails with a database error for the named artists."""

    def __init__(self) -> None:
        super().__init__()
        self.broken: set[str] = set()

    def record_outcome(self, artist_id: str, outcome: ArtistLinkOutcome) -> None:
        if self.artists[artist_id].name in self.broken:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        super().record_outcome(artist_id, outcome)


class _Rig:
    def __init__(self) -> None:
        self.progress = FakeTaskProgressRepository()
        self.logs = FakeSystemLogRepository()
        self.repo = _Repo()
        self.mb = _Mb(
            artists={
                NIRVANA: {"id": NIRVANA, "name": "Nirvana", "sort-name": "Nirvana"},
                SOUNDGARDEN: {"id": SOUNDGARDEN, "name": "Soundgarden", "sort-name": "Soundgarden"},
                OZZY_OSBOURNE: {"id": OZZY_OSBOURNE, "name": "Ozzy Osbourne"},
            }
        )
        self.order: list[str] = []
        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.__exit__.return_value = False
        self.conn.commit.side_effect = lambda: self.order.append("commit")
        self.conn.rollback.side_effect = lambda: self.order.append("rollback")

    def factory(self, _conn: object) -> SimpleNamespace:
        return SimpleNamespace(artist_linking=self.repo)

    def artist(self, name: str, *tags: str) -> Artist:
        return self.repo.add(
            Artist(
                id=str(uuid4()),
                name=name,
                sort_name=name,
                normalized_name=normalize_artist(name),
                needs_enhancement=False,
            ),
            LinkEvidence(tag_counts=tuple((tag, 1) for tag in tags)),
        )

    def completed_row(self) -> TaskProgress:
        rows = [u for u in self.progress.received_upserts if u.status == TaskStatus.COMPLETED]
        assert rows, "the run wrote no COMPLETED row"
        return rows[-1]


@pytest.fixture
def rig() -> Iterator[_Rig]:
    r = _Rig()
    with (
        patch("backend.tasks._task_run.connect_sync", return_value=MagicMock()),
        patch("backend.tasks._task_run.PgTaskProgressRepository", return_value=r.progress),
        patch("backend.tasks._task_run.PgSystemLogRepository", return_value=r.logs),
        patch("backend.tasks.artist_linking_tasks.connect_sync", return_value=r.conn),
        patch("backend.tasks.artist_linking_tasks.RepositoryFactory", side_effect=r.factory),
        patch("backend.tasks.artist_linking_tasks.PgMusicBrainzCacheRepository"),
        patch(
            "backend.tasks.artist_linking_tasks.MusicBrainzApiClient",
            side_effect=lambda *args, **kwargs: r.mb,
        ),
    ):
        yield r


def _run() -> dict[str, int]:
    from backend.tasks.artist_linking_tasks import link_local_artists_task

    result: dict[str, int] = link_local_artists_task.call_local()
    return result


# --- A normal run ----------------------------------------------------------------------------


def test_a_run_links_or_records_every_due_artist(rig: _Rig) -> None:
    nirvana = rig.artist("Nirvana", NIRVANA)
    ozzy = rig.artist("Ozzy", OZZY_OSBOURNE)
    bare = rig.artist("Marie Osmond")

    result = _run()

    assert rig.repo.artists[nirvana.id].mbid == NIRVANA
    assert rig.repo.artists[ozzy.id].mb_lookup_outcome == ArtistLinkOutcome.TAG_MISMATCH
    assert rig.repo.artists[bare.id].mb_lookup_outcome == ArtistLinkOutcome.NO_EVIDENCE
    assert rig.repo.list_due() == []
    assert result == {
        "artists": 3,
        **NO_LINK,
        "linked": 1,
        "tag_mismatch": 1,
        "no_evidence": 1,
        "failed": 0,
        "stopped_early": 0,
    }


def test_the_run_writes_a_completed_artist_linking_row(rig: _Rig) -> None:
    rig.artist("Nirvana", NIRVANA)

    _run()

    row = rig.completed_row()
    assert row.task_type == TaskType.ARTIST_LINKING
    assert (row.progress_data["processed"], row.progress_data["total"]) == (1, 1)
    assert (row.progress_data["linked"], row.progress_data["stopped_early"]) == (1, 0)


def test_progress_is_reported_after_every_artist(rig: _Rig) -> None:
    rig.artist("Nirvana", NIRVANA)
    rig.artist("Soundgarden", SOUNDGARDEN)

    _run()

    running = [u for u in rig.progress.received_upserts if u.status == TaskStatus.RUNNING]
    assert [u.progress_data["processed"] for u in running] == [0, 1, 2]
    assert {u.progress_data["total"] for u in running} == {2}
    assert running[0].task_type == TaskType.ARTIST_LINKING


def test_each_artist_is_committed_on_its_own(rig: _Rig) -> None:
    rig.artist("Nirvana", NIRVANA)
    rig.artist("Soundgarden", SOUNDGARDEN)

    _run()

    assert rig.order == ["commit", "commit", "commit"]  # the listing, then one per artist


def test_nothing_due_completes_at_zero_without_asking_musicbrainz(rig: _Rig) -> None:
    result = _run()

    assert result == {"artists": 0, **NO_LINK, "failed": 0, "stopped_early": 0}
    assert rig.mb.calls == []
    row = rig.completed_row()
    assert (row.progress_data["processed"], row.progress_data["total"]) == (0, 0)


# --- Failures on one artist ------------------------------------------------------------------


@pytest.mark.parametrize("status", [403, 429, 503])
def test_an_http_failure_leaves_the_artist_due(rig: _Rig, status: int) -> None:
    rig.mb.refuse[SOUNDGARDEN] = status
    soundgarden = rig.artist("Soundgarden", SOUNDGARDEN)
    nirvana = rig.artist("Nirvana", NIRVANA)

    result = _run()

    assert [a.id for a in rig.repo.list_due()] == [soundgarden.id]
    assert rig.repo.artists[soundgarden.id].mb_lookup_outcome is None
    assert rig.repo.artists[nirvana.id].mbid == NIRVANA
    assert (result["failed"], result["linked"], result["stopped_early"]) == (1, 1, 0)
    assert rig.order == ["commit", "commit", "rollback"]  # "nirvana" sorts first
    assert rig.completed_row().progress_data["failed"] == 1


def test_a_network_error_leaves_the_artist_due(rig: _Rig) -> None:
    rig.mb.network_down.add(SOUNDGARDEN)
    soundgarden = rig.artist("Soundgarden", SOUNDGARDEN)

    result = _run()

    assert [a.id for a in rig.repo.list_due()] == [soundgarden.id]
    assert result["failed"] == 1


def test_a_database_error_rolls_that_artist_back_and_the_run_goes_on(rig: _Rig) -> None:
    rig.repo.broken.add("Marie Osmond")
    bare = rig.artist("Marie Osmond")
    rig.artist("Nirvana", NIRVANA)

    result = _run()

    assert (result["failed"], result["linked"]) == (1, 1)
    assert [a.id for a in rig.repo.list_due()] == [bare.id]
    assert rig.order == ["commit", "rollback", "commit"]  # "marie osmond" sorts first


def test_ten_failures_in_a_row_stop_the_run_and_the_rest_stay_due(rig: _Rig) -> None:
    for n in range(10, 22):  # twelve artists, every lookup refused
        rig.mb.refuse[_mbid(n)] = 503
        rig.artist(f"Band {n}", _mbid(n))

    result = _run()

    assert (result["failed"], result["stopped_early"]) == (10, 1)
    assert len(rig.repo.list_due()) == 12  # the ten that failed and the two never tried
    assert len(rig.mb.asked) == 10  # the last two were never looked up
    row = rig.completed_row()
    assert (row.progress_data["processed"], row.progress_data["total"]) == (10, 12)


def test_a_success_resets_the_failure_count(rig: _Rig) -> None:
    for n in range(10, 19):  # nine failures ("band 1x" sorts first) ...
        rig.mb.refuse[_mbid(n)] = 503
        rig.artist(f"Band {n}", _mbid(n))
    rig.artist("Nirvana", NIRVANA)  # ... then a link ...
    for n in range(30, 39):  # ... then nine more ("tribe 3x" sorts last)
        rig.mb.refuse[_mbid(n)] = 503
        rig.artist(f"Tribe {n}", _mbid(n))

    result = _run()

    assert (result["failed"], result["linked"], result["stopped_early"]) == (18, 1, 0)


def test_artists_decided_without_a_lookup_do_not_reset_the_failure_count(rig: _Rig) -> None:
    # Dev DB: untagged artists sit between the tagged ones; the longest tagged run is 14.
    for n in range(10, 20):  # ten refused lookups, each followed by an untagged artist
        rig.mb.refuse[_mbid(n)] = 503
        rig.artist(f"Band {n}", _mbid(n))
        rig.artist(f"Band {n} Tribute", *([UNKNOWN] if n % 2 else []))
    nirvana = rig.artist("Nirvana", NIRVANA)  # sorts last: never reached

    result = _run()

    assert (
        result["failed"],
        result["no_evidence"],
        result["special_purpose"],
        result["stopped_early"],
    ) == (10, 5, 4, 1)
    assert rig.repo.artists[nirvana.id].mbid is None


# --- The envelope ----------------------------------------------------------------------------


def test_a_failed_listing_fails_the_run(rig: _Rig) -> None:
    rig.artist("Nirvana", NIRVANA)

    with (
        patch(
            "backend.tasks.artist_linking_tasks.RepositoryFactory",
            side_effect=RuntimeError("database gone"),
        ),
        pytest.raises(RuntimeError, match="database gone"),
    ):
        _run()

    statuses = [u.status for u in rig.progress.received_upserts]
    assert statuses[-1] == TaskStatus.FAILED
    assert TaskStatus.COMPLETED not in statuses


REGISTRY_PROBE = """
import json
import backend.tasks.huey_app as library
print(json.dumps(sorted(library.huey._registry._registry)))
"""


@pytest.mark.slow
def test_the_library_worker_registers_the_linking_task() -> None:
    """Importing only huey_app (what the consumer does) registers the task, or a queued run
    cannot be deserialised (event-graph rule EV06)."""
    probe = subprocess.run(
        [sys.executable, "-c", REGISTRY_PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    registered = json.loads(probe.stdout.strip().splitlines()[-1])
    assert "backend.tasks.artist_linking_tasks.link_local_artists_task" in registered
