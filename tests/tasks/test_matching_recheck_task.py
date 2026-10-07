"""Acceptance tests: the targeted re-check task (spec 2026-10-05 §4.2; AUD-R022 D1, D2).

rematch_undecided_task(scope) runs inside task_run as TaskType.MATCHING_RECHECK:
- the watermark is the started_at of the newest COMPLETED matching_recheck row. FAILED and
  RUNNING rows, and other task types, never count. The run's own start, taken before any query,
  is the next watermark;
- scope "changed" rewinds the undecided artists named by files indexed or gone missing after the
  watermark, then every undecided song under those artists, whatever the artist's own status.
  Scope "all", or no watermark, rewinds every undecided item. Decided items never move;
- the rewind runs in one transaction, committed before the fan-out. The fan-out queues
  artist_matching_task once per playlist with pending work, each through enqueue_or_log,
  even when the wave is empty.

The repositories are the in-memory fakes; the Pg side of every query is in
tests/integration/test_matching_recheck_pg.py.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import (
    EnrichmentStatus,
    FileStatus,
    MatchStatus,
    MatchTier,
    ReasonCode,
    TaskStatus,
    TaskType,
)
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.system import TaskProgress
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.system_logs import FakeSystemLogRepository
from tests.fakes.task_progress import FakeTaskProgressRepository

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DECIDED = [MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED, MatchStatus.MANUAL_REJECTED]


class _WaveFiles(FakeLibraryFileRepository):
    """Records each wave query's time, and when the first one was asked."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[datetime] = []
        self.first_asked_at: datetime | None = None

    def normalized_artist_names_changed_since(self, when: datetime) -> set[str]:
        if self.first_asked_at is None:
            self.first_asked_at = datetime.now(UTC)
        self.asked.append(when)
        return super().normalized_artist_names_changed_since(when)


class _Rig:
    """Fakes behind every collaborator of rematch_undecided_task, plus recorders."""

    def __init__(self) -> None:
        self.progress = FakeTaskProgressRepository()
        self.logs = FakeSystemLogRepository()
        self.handoff_logs = FakeSystemLogRepository()
        self.files = _WaveFiles()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = FakeBroadcastTrackIdentityRepository()
        self.queued: list[str] = []
        self.refuse: set[str] = set()
        self.order: list[str] = []
        self.connects: list[dict[str, Any]] = []
        self.conn = MagicMock()
        self.conn.__enter__.return_value = self.conn
        self.conn.__exit__.return_value = False
        self.conn.commit.side_effect = lambda: self.order.append("commit")

    # Wiring ---------------------------------------------------------------------------------

    def connect(self, _url: str, **kwargs: Any) -> MagicMock:
        self.connects.append(kwargs)
        return self.conn

    def factory(self, _conn: object) -> SimpleNamespace:
        return SimpleNamespace(
            library_files=self.files,
            broadcast_artists=self.artists,
            broadcast_identities=self.songs,
            task_progress=self.progress,
            system_logs=self.handoff_logs,
        )

    def enqueue(self, playlist_id: str) -> None:
        self.order.append("enqueue")
        if playlist_id in self.refuse:
            raise sqlite3.OperationalError("database is locked")
        self.queued.append(playlist_id)

    # Seeding --------------------------------------------------------------------------------

    def finished_run(
        self,
        started_at: datetime,
        *,
        status: TaskStatus = TaskStatus.COMPLETED,
        task_type: TaskType | None = None,
    ) -> None:
        self.progress.upsert(
            TaskProgress(
                task_id=uuid4().hex,
                task_type=task_type or TaskType.MATCHING_RECHECK,
                status=status,
                progress_data={},
                started_at=started_at,
                updated_at=started_at,
                completed_at=None if status == TaskStatus.RUNNING else started_at,
            )
        )
        self.progress.received_upserts.clear()

    def changed_file(
        self, name: str, *, indexed_at: datetime, missing_since: datetime | None = None
    ) -> None:
        self.files.upsert(
            LibraryFile(
                id=uuid4(),
                file_path=f"/music/{uuid4().hex}.flac",
                format="flac",
                enrichment_status=EnrichmentStatus.ENRICHED,
                file_status=FileStatus.MISSING if missing_since else FileStatus.PRESENT,
                indexed_at=indexed_at,
                missing_since=missing_since,
                audio=AudioMetadata(
                    track_title="Song", artist_name=name, normalized_artist_name=name
                ),
            )
        )

    def artist(self, name: str, status: MatchStatus) -> BroadcastArtist:
        return self.artists.upsert(
            BroadcastArtist(
                id=uuid4(),
                original_name=name,
                normalized_name=name,
                match_status=status,
                reason_code=None if status == MatchStatus.PENDING else ReasonCode.LOW_CONFIDENCE,
            )
        )

    def song(
        self, artist: BroadcastArtist, status: MatchStatus, playlist_id: UUID | None = None
    ) -> BroadcastTrackIdentity:
        title = uuid4().hex
        decided = status != MatchStatus.PENDING
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=artist.id,
                original_title=title,
                normalized_title=title,
                normalized_signature=f"{artist.normalized_name}:{title}",
                match_status=status,
                match_tier=MatchTier.LOCAL_FILE_FUZZY if decided else None,
                reason_code=ReasonCode.LOW_CONFIDENCE if decided else None,
                reason_detail="stale reason" if decided else None,
            )
        )
        if playlist_id is not None:
            self.songs.register_playlist_identity(playlist_id, song.id)
            self.artists.register_playlist_artist(playlist_id, artist.id)
        return song

    # Reading --------------------------------------------------------------------------------

    def stored_artist(self, artist: BroadcastArtist) -> BroadcastArtist:
        stored = self.artists.get_by_id(artist.id)
        assert stored is not None
        return stored

    def stored_song(self, song: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return stored

    def completed_row(self) -> TaskProgress:
        rows = [t for t in self.progress.received_upserts if t.status == TaskStatus.COMPLETED]
        assert rows, "the run wrote no COMPLETED row"
        return rows[-1]


@pytest.fixture
def rig() -> Iterator[_Rig]:
    r = _Rig()
    with (
        patch("backend.tasks._task_run.connect_sync", return_value=MagicMock()),
        patch("backend.tasks._task_run.PgTaskProgressRepository", return_value=r.progress),
        patch("backend.tasks._task_run.PgSystemLogRepository", return_value=r.logs),
        patch("backend.tasks.matching_recheck_tasks.connect_sync", side_effect=r.connect),
        patch("backend.tasks.matching_recheck_tasks.RepositoryFactory", side_effect=r.factory),
        patch(
            "backend.tasks.matching_recheck_tasks.PgSystemLogRepository",
            return_value=r.handoff_logs,
            create=True,
        ),
        patch("backend.tasks.artist_matching_tasks.artist_matching_task", side_effect=r.enqueue),
    ):
        yield r


def _run(scope: str) -> dict[str, int]:
    from backend.tasks.matching_recheck_tasks import rematch_undecided_task

    result: dict[str, int] = rematch_undecided_task.call_local(scope)
    return result


# --- The wave ------------------------------------------------------------------------------


def test_changed_scope_rewinds_the_artists_of_changed_files_only(rig: _Rig) -> None:
    rig.finished_run(T0)
    rig.changed_file("abba", indexed_at=T0 + HOUR)
    rig.changed_file("queen", indexed_at=T0 - HOUR)
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)
    queen = rig.artist("queen", MatchStatus.AUTO_REJECTED)
    abba_song = rig.song(abba, MatchStatus.AUTO_REJECTED)
    queen_song = rig.song(queen, MatchStatus.NEEDS_REVIEW)

    result = _run("changed")

    assert rig.stored_artist(abba).match_status == MatchStatus.PENDING
    assert rig.stored_artist(abba).reason_code is None
    rewound = rig.stored_song(abba_song)
    assert rewound.match_status == MatchStatus.PENDING
    assert (rewound.match_tier, rewound.reason_code, rewound.reason_detail) == (None, None, None)
    assert rig.stored_artist(queen).match_status == MatchStatus.AUTO_REJECTED
    assert rig.stored_song(queen_song).match_status == MatchStatus.NEEDS_REVIEW
    counts = {k: result[k] for k in ("artists_rewound", "songs_rewound", "playlists")}
    assert counts == {"artists_rewound": 1, "songs_rewound": 1, "playlists": 0}


def test_a_file_gone_missing_puts_its_artist_in_the_wave(rig: _Rig) -> None:
    rig.finished_run(T0)
    rig.changed_file("abba", indexed_at=T0 - HOUR, missing_since=T0 + HOUR)
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)

    _run("changed")

    assert rig.stored_artist(abba).match_status == MatchStatus.PENDING


def test_failed_running_and_timed_out_runs_do_not_move_the_watermark(rig: _Rig) -> None:
    rig.finished_run(T0)
    rig.finished_run(T0 + 2 * HOUR, status=TaskStatus.FAILED)
    rig.finished_run(T0 + 3 * HOUR, status=TaskStatus.RUNNING)
    rig.finished_run(T0 + 4 * HOUR, task_type=TaskType.MB_ENRICHMENT)
    rig.finished_run(T0 + 5 * HOUR, status=TaskStatus.TIMEOUT)
    rig.changed_file("abba", indexed_at=T0 + HOUR)
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)

    _run("changed")

    assert rig.files.asked == [T0]
    assert rig.stored_artist(abba).match_status == MatchStatus.PENDING


def test_without_a_watermark_everything_undecided_is_rechecked(rig: _Rig) -> None:
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)
    song = rig.song(abba, MatchStatus.AUTO_REJECTED)

    _run("changed")

    assert rig.files.asked == []
    assert rig.stored_artist(abba).match_status == MatchStatus.PENDING
    assert rig.stored_song(song).match_status == MatchStatus.PENDING


def test_scope_all_rechecks_everything_despite_a_watermark(rig: _Rig) -> None:
    rig.finished_run(T0)
    abba = rig.artist("abba", MatchStatus.AUTO_REJECTED)  # no changed file names abba
    song = rig.song(abba, MatchStatus.NEEDS_REVIEW)

    _run("all")

    assert rig.files.asked == []
    assert rig.stored_artist(abba).match_status == MatchStatus.PENDING
    assert rig.stored_song(song).match_status == MatchStatus.PENDING


@pytest.mark.parametrize("status", DECIDED)
def test_decided_artists_and_songs_are_never_rewound(rig: _Rig, status: MatchStatus) -> None:
    artist = rig.artist("abba", status)
    song = rig.song(artist, status)

    result = _run("all")

    assert rig.stored_artist(artist).match_status == status
    assert rig.stored_song(song).match_status == status
    assert rig.stored_song(song).reason_code == ReasonCode.LOW_CONFIDENCE
    assert (result["artists_rewound"], result["songs_rewound"]) == (0, 0)


def test_songs_follow_an_artist_in_the_wave_whatever_its_status(rig: _Rig) -> None:
    rig.finished_run(T0)
    rig.changed_file("abba", indexed_at=T0 + HOUR)
    abba = rig.artist("abba", MatchStatus.AUTO_MATCHED)
    review = rig.song(abba, MatchStatus.NEEDS_REVIEW)

    _run("changed")

    assert rig.stored_artist(abba).match_status == MatchStatus.AUTO_MATCHED
    assert rig.stored_song(review).match_status == MatchStatus.PENDING


def test_an_empty_wave_rewinds_nothing_but_still_fans_out(rig: _Rig) -> None:
    rig.finished_run(T0)
    playlist = uuid4()
    queen = rig.artist("queen", MatchStatus.AUTO_MATCHED)
    review = rig.song(queen, MatchStatus.NEEDS_REVIEW, playlist)
    rig.song(queen, MatchStatus.PENDING, playlist)  # left pending by a failed downstream run
    idle = rig.artist("blondie", MatchStatus.NEEDS_REVIEW)  # undecided, but not in the wave

    result = _run("changed")

    assert rig.files.asked == [T0]
    assert rig.stored_artist(idle).match_status == MatchStatus.NEEDS_REVIEW
    assert rig.stored_song(review).match_status == MatchStatus.NEEDS_REVIEW
    assert (result["artists_rewound"], result["songs_rewound"]) == (0, 0)
    assert rig.queued == [str(playlist)]


# --- The fan-out ---------------------------------------------------------------------------


def test_one_hand_off_per_playlist_with_pending_work(rig: _Rig) -> None:
    with_artist, with_song, done = uuid4(), uuid4(), uuid4()
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)  # rewound: its playlist has work
    rig.song(abba, MatchStatus.AUTO_MATCHED, with_artist)
    queen = rig.artist("queen", MatchStatus.AUTO_MATCHED)
    rig.song(queen, MatchStatus.AUTO_REJECTED, with_song)  # two rewound songs, one playlist
    rig.song(queen, MatchStatus.NEEDS_REVIEW, with_song)
    kiss = rig.artist("kiss", MatchStatus.MANUAL_MATCHED)
    rig.song(kiss, MatchStatus.MANUAL_MATCHED, done)

    result = _run("all")

    assert sorted(rig.queued) == sorted([str(with_artist), str(with_song)])
    assert result["playlists"] == 2


def test_a_playlist_pending_on_both_sides_is_queued_once(rig: _Rig) -> None:
    both = uuid4()
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)  # rewound: artist side reports `both`
    rig.song(abba, MatchStatus.AUTO_REJECTED, both)  # rewound: song side reports `both`

    result = _run("all")

    assert rig.queued == [str(both)]
    assert result["playlists"] == 1


def test_the_rewind_is_committed_before_the_first_hand_off(rig: _Rig) -> None:
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)
    rig.song(abba, MatchStatus.NEEDS_REVIEW, uuid4())

    _run("all")

    assert rig.order == ["commit", "enqueue"]


def test_the_rewind_runs_in_a_transaction(rig: _Rig) -> None:
    abba = rig.artist("abba", MatchStatus.PENDING)
    rig.song(abba, MatchStatus.PENDING, uuid4())

    _run("changed")

    assert rig.connects
    assert rig.connects[0].get("autocommit", False) is False


def test_a_refused_hand_off_is_logged_and_the_rest_still_queue(rig: _Rig) -> None:
    first, second = sorted((uuid4(), uuid4()), key=str)
    abba = rig.artist("abba", MatchStatus.PENDING)
    rig.song(abba, MatchStatus.PENDING, first)
    rig.song(abba, MatchStatus.PENDING, second)
    rig.refuse = {str(first)}

    _run("all")

    assert rig.queued == [str(second)]
    refused = [
        log for log in rig.handoff_logs.all if log.message == "artist_matching_task_enqueue_failed"
    ]
    assert len(refused) == 1
    assert refused[0].trace_id == rig.completed_row().task_id


# --- The envelope --------------------------------------------------------------------------


def test_a_failed_run_writes_failed_and_hands_off_nothing(rig: _Rig) -> None:
    abba = rig.artist("abba", MatchStatus.NEEDS_REVIEW)
    rig.song(abba, MatchStatus.PENDING, uuid4())

    with (
        patch(
            "backend.tasks.matching_recheck_tasks.RepositoryFactory",
            side_effect=RuntimeError("database gone"),
        ),
        pytest.raises(RuntimeError, match="database gone"),
    ):
        _run("all")

    statuses = [t.status for t in rig.progress.received_upserts]
    assert statuses[-1] == TaskStatus.FAILED
    assert TaskStatus.COMPLETED not in statuses
    assert rig.queued == []


def test_an_unknown_scope_fails_the_run(rig: _Rig) -> None:
    with pytest.raises(ValueError):
        _run("everything")

    statuses = [t.status for t in rig.progress.received_upserts]
    assert statuses
    assert statuses[-1] == TaskStatus.FAILED
    assert TaskStatus.COMPLETED not in statuses
    assert rig.queued == []


def test_the_runs_own_start_is_the_next_watermark(rig: _Rig) -> None:
    rig.finished_run(T0)
    before = datetime.now(UTC)

    _run("changed")

    first = rig.completed_row()
    assert TaskType.MATCHING_RECHECK.value == "matching_recheck"
    assert first.task_type == TaskType.MATCHING_RECHECK
    assert rig.files.first_asked_at is not None
    assert before <= first.started_at <= rig.files.first_asked_at

    _run("changed")

    assert rig.files.asked == [T0, first.started_at]


def test_an_all_run_also_advances_the_watermark(rig: _Rig) -> None:
    rig.finished_run(T0)

    _run("all")
    everything = rig.completed_row()
    _run("changed")

    assert rig.files.asked == [everything.started_at]


REGISTRY_PROBE = """
import json
import backend.tasks.huey_app as library
print(json.dumps(sorted(library.huey._registry._registry)))
"""


@pytest.mark.slow
def test_the_library_worker_registers_the_recheck_task() -> None:
    """Importing only huey_app (what the consumer does) registers the task, or a queued
    re-check cannot be deserialised (event-graph rule EV06)."""
    probe = subprocess.run(
        [sys.executable, "-c", REGISTRY_PROBE],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    registered = json.loads(probe.stdout.strip().splitlines()[-1])
    assert "backend.tasks.matching_recheck_tasks.rematch_undecided_task" in registered
