"""Characterisation, artist model phase 0a (SPEC S3): the artist stage when MusicBrainz is down.

Every MusicBrainz call raises what the real client raises once its own retries give up on a
503: ``httpx.HTTPStatusError``. Today the artist stage asks MusicBrainz only about names that
look cut off by the log's fixed-width field ("Invariant 3"), and only the coalesced pre-search
catches that error; the per-name search does not. So:

- strategy by strategy: the mapping rules and the local catalog never call MusicBrainz; the
  MusicBrainz strategy raises for any name; behind the cut-off gate it raises for a cut-off
  name only, and skips the others without a call; the deferred-retry strategy parks any name
  in review as DEFERRED_RETRY; the coalesced pre-search drops the failed name and carries on;
- the stage (``artist_matching_task``, on PostgreSQL): a playlist with no cut-off name never
  calls MusicBrainz and finishes, an exact catalog name auto-matched and any other name parked
  in review as DEFERRED_RETRY; a playlist with a cut-off name fails as a whole, and every one of
  its names stays pending.

AUD-R036 retires Invariant 3 and AUD-R042 extends the outage path to the artist searches;
phase 4's new linker replaces this stage and relocks this file.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any, NoReturn
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from huey.storage import MemoryStorage
from mutagen.id3 import ID3, TIT2, TPE1
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.domain.broadcast import BroadcastArtist
from backend.domain.catalog import Artist
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.services.artist_matching_service import (
    DeferredRetryStrategy,
    MappingRuleStrategy,
    MusicBrainzApiStrategy,
    NormalizationStrategy,
    TruncatedNameMbStrategy,
    coalesce_artist_searches,
)
from backend.services.matching_utils import is_likely_truncated
from backend.services.normalization import normalize_artist
from backend.tasks import artist_matching_tasks
from backend.tasks.huey_app import huey
from backend.tasks.ingestion_tasks import ingestion_task
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan, put

type Conn = psycopg.Connection[DictRow]

MAX_LEN = 30  # Settings.broadcast_name_max_len
KNOWN = "KNOWN BAND"
UNKNOWN = "ZZYZX QUARTET"
CUT_OFF = "QUIXOTIC VELVET HARMONY ORCHES"


@pytest.fixture(scope="module", autouse=True)
def queue_in_memory() -> Iterator[None]:
    """The hand-offs the writes queue land in memory, never in a worker's queue file (from the
    main checkout, a -n 0 run's queue file is the dev worker's)."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey, "storage", MemoryStorage(huey.name))
    yield
    patch.undo()


class MusicBrainzDown:
    """A MusicBrainz client whose every call fails as the real one does on a lasting 503."""

    live_fetches = 0
    cache_hits = 0

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _down(self, call: str) -> NoReturn:
        self.calls.append(call)
        request = httpx.Request("GET", "https://musicbrainz.org/ws/2/")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError(
            "Server error '503 Service Unavailable'", request=request, response=response
        )

    def search_artist(self, name: str) -> NoReturn:
        self._down(f"search_artist:{name}")

    def lookup_artist(self, mbid: str) -> NoReturn:
        self._down(f"lookup_artist:{mbid}")

    def lookup_release(self, mbid: str) -> NoReturn:
        self._down(f"lookup_release:{mbid}")

    def lookup_recording(self, mbid: str) -> NoReturn:
        self._down(f"lookup_recording:{mbid}")

    def search_recording(self, artist_mbid: str, title: str, limit: int = 10) -> NoReturn:
        self._down(f"search_recording:{artist_mbid}:{title}")

    def search_recordings_by_mbids(self, mbids: Any) -> NoReturn:
        self._down("search_recordings_by_mbids")


def _name(original: str) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(), original_name=original, normalized_name=normalize_artist(original)
    )


CATALOG = [Artist(id="local-known-band", name="Known Band", sort_name="Known Band")]


def test_the_names_are_cut_off_or_not_as_named() -> None:
    assert [is_likely_truncated(n, MAX_LEN) for n in (KNOWN, UNKNOWN, CUT_OFF)] == [
        False,
        False,
        True,
    ]


def test_mapping_rules_and_the_local_catalog_never_call_musicbrainz() -> None:
    known, unknown, cut_off = _name(KNOWN), _name(UNKNOWN), _name(CUT_OFF)
    rules = MappingRuleStrategy([])
    local = NormalizationStrategy(CATALOG)

    assert [rules.apply(n) for n in (known, unknown, cut_off)] == [None, None, None]
    match = local.apply(known)
    assert match is not None
    assert (match.status, match.tier, match.confidence_score, match.target_id) == (
        MatchStatus.AUTO_MATCHED,
        MatchTier.NORMALIZATION,
        100.0,
        "local-known-band",
    )
    assert (local.apply(unknown), local.apply(cut_off)) == (None, None)


@pytest.mark.parametrize("original", [KNOWN, UNKNOWN, CUT_OFF])
def test_the_musicbrainz_strategy_raises_for_any_name(original: str) -> None:
    mb = MusicBrainzDown()

    with pytest.raises(httpx.HTTPStatusError):
        MusicBrainzApiStrategy(mb).apply(_name(original))

    assert mb.calls == [f"search_artist:{original}"]


def test_the_cut_off_gate_raises_for_a_cut_off_name_and_skips_the_rest() -> None:
    mb = MusicBrainzDown()
    gated = TruncatedNameMbStrategy(MusicBrainzApiStrategy(mb), max_len=MAX_LEN)

    assert (gated.apply(_name(KNOWN)), gated.apply(_name(UNKNOWN))) == (None, None)
    assert mb.calls == []
    with pytest.raises(httpx.HTTPStatusError):
        gated.apply(_name(CUT_OFF))
    assert mb.calls == [f"search_artist:{CUT_OFF}"]


@pytest.mark.parametrize("original", [KNOWN, UNKNOWN, CUT_OFF])
def test_deferred_retry_parks_any_name_in_review(original: str) -> None:
    result = DeferredRetryStrategy().apply(_name(original))

    assert result is not None
    assert (result.status, result.reason_code, result.confidence_score) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.DEFERRED_RETRY,
        0.0,
    )


def test_the_coalesced_search_drops_a_failed_name_and_carries_on() -> None:
    mb = MusicBrainzDown()

    assert coalesce_artist_searches([_name(CUT_OFF)], mb) == ({}, 1)
    assert mb.calls == [f"search_artist:{CUT_OFF}"]


# --- the stage, through artist_matching_task on PostgreSQL -------------------------------------


@pytest.fixture
def conn(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Conn]:
    """A connection to the test database, which the task entry points' settings name too."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection
    get_settings.cache_clear()


@pytest.fixture
def musicbrainz_down(monkeypatch: pytest.MonkeyPatch) -> MusicBrainzDown:
    mb = MusicBrainzDown()
    monkeypatch.setattr(
        artist_matching_tasks, "MusicBrainzApiClient", lambda *_a, **_k: nullcontext(mb)
    )
    return mb


def _ingested(conn: Conn, tmp_path: Path, *names: str) -> UUID:
    """The catalog holds Known Band (from the scan); a log of *names* is ingested at a station.
    Returns the playlist."""
    put("well_tagged.mp3", tmp_path / "library" / "known.mp3")
    tags = ID3(str(tmp_path / "library" / "known.mp3"))
    tags.clear()
    tags.add(TPE1(encoding=3, text=["Known Band"]))
    tags.add(TIT2(encoding=3, text=["Known Song"]))
    tags.save()
    full_scan(conn, tmp_path / "library")
    station = seed.station(conn, "AC")
    conn.commit()
    rows = "".join(f"{n},SONG {i},1995-03-14 08:0{i}:00\n" for i, n in enumerate(names))
    log = f"Artist,Title,Played\n{rows}".encode()
    ingestion_task.call_local(log, "outage.csv", str(station), "phase-0a-outage")
    row = conn.execute("SELECT id FROM playlists WHERE station_id = %s", (station,)).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


def _statuses(conn: Conn) -> dict[str, tuple[str, str | None]]:
    """Each log name's status and reason after the stage."""
    rows = conn.execute(
        "SELECT original_name, match_status, reason_code FROM broadcast_artists"
    ).fetchall()
    return {r["original_name"]: (r["match_status"], r["reason_code"]) for r in rows}


@pytest.mark.integration
def test_a_stage_without_cut_off_names_never_calls_musicbrainz(
    conn: Conn, tmp_path: Path, musicbrainz_down: MusicBrainzDown
) -> None:
    playlist = _ingested(conn, tmp_path, KNOWN, UNKNOWN)
    assert get_settings().broadcast_name_max_len == MAX_LEN

    artist_matching_tasks.artist_matching_task.call_local(str(playlist))

    assert musicbrainz_down.calls == []
    assert _statuses(conn) == {
        KNOWN: (MatchStatus.AUTO_MATCHED, None),
        UNKNOWN: (MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY),
    }


@pytest.mark.integration
def test_a_stage_with_a_cut_off_name_fails_and_decides_nothing(
    conn: Conn, tmp_path: Path, musicbrainz_down: MusicBrainzDown
) -> None:
    playlist = _ingested(conn, tmp_path, KNOWN, UNKNOWN, CUT_OFF)
    assert get_settings().broadcast_name_max_len == MAX_LEN

    with pytest.raises(httpx.HTTPStatusError):
        artist_matching_tasks.artist_matching_task.call_local(str(playlist))

    assert musicbrainz_down.calls == [f"search_artist:{CUT_OFF}", f"search_artist:{CUT_OFF}"]
    assert _statuses(conn) == {
        KNOWN: (MatchStatus.PENDING, None),
        UNKNOWN: (MatchStatus.PENDING, None),
        CUT_OFF: (MatchStatus.PENDING, None),
    }
