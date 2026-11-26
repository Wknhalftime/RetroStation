"""The MusicBrainz cache fill: which calls a run still owes, and one batch of them (phase 4.1
PR 2; SPEC S3, S4, S7, E1, E2, E4, E5; D3, D4, D8; M1-M11 as the coordinator read them;
AUD-R091).

``plan_run`` is a query: every call the run still has room for, in tier order (payoff, C68, C18,
confirmations, work links, tail, then the alias step), skipping any query with a live cache
entry (C42) or tried and not answered earlier in the run, one call per cache key (M9), each tier
within its cap for the run. ``run_batch`` makes the first of them, one at a time, committing
each call's rows as they land (R8), and returns the run's next state: what to re-check, and
whether the run goes on.

PR 2b's request budget (D9, D10, D11, D12; AUD-R093): each call is one request (the task hands
the batch a one-attempt view of the client), a refusal is followed by a pause before the next
request, and a run hands on no batch once its minutes are spent. A batch's last row reports the
run's requests, its minutes and how it ended, and the run's failed calls when there are any.
PR 2c (D18, D19; AUD-R094): a refusal that no request follows ends the batch and the run there,
so the refused call is the run's last call.

PR 3's work links (S5; D13, D15, D16; AUD-R092): a tier between confirmations and the tail looks
up the releases the work list names, the most covering first, and writes each track's
``recording-work:`` row with the call. A batch's plan is fixed, so a recording a 404 or a
release without it (D15) left unlinked is asked through another release the next time the work
list is planned: the next batch, or the next run.
"""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import Any, Protocol
from uuid import uuid4

import httpx
import structlog

from backend.domain.cache_fill import (
    FillRun,
    ManualTitle,
    MusicBrainzRefusingError,
    NamePair,
    QueuedPair,
    UnlinkedRecording,
    UntaggedFile,
)
from backend.domain.enums import ArtistNameSource, FillEnd, FillTier
from backend.domain.matching import ARTIST_UNDECIDED_STATUSES
from backend.domain.system import MusicBrainzCache
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_names import ArtistNameRepository
from backend.repositories.cache_fill_work import CacheFillWorkRepository
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository
from backend.services.cache_fill_constants import ALIASES_PER_CALL, REFUSALS_TO_FAIL, FillLimits
from backend.services.cache_fill_words import has_joiner, name_words, search_title
from backend.services.library_enrichment_service import performed_work_of
from backend.services.matching_utils import is_likely_truncated
from backend.services.mb_client import (
    MusicBrainzClientProtocol,
    aliases_key,
    name_title_key,
    recording_search_key,
)
from backend.services.mb_types import MbAlias, MbRelease
from backend.services.normalization import normalize_artist
from backend.services.recording_search import is_transient_mb_failure
from backend.services.title_scoring import broadcast_title_variants, credit_stripped_title

logger = structlog.get_logger()

type AliasWriter = Callable[[Mapping[str, list[MbAlias]]], None]
type WorkLinkWriter = Callable[[MbRelease], None]

STOPPED = "stopped (MusicBrainz not answering)"
"""D8: how a cut-off run's last row reads, after its tier."""

TIME_UP = "stopped (2 hours up)"
"""D12: how a run D11's time limit ended reads, after the tier the rest waits in."""

CALLS_FAILED = "mb_fill_calls_failed"
"""D10: the warning on a run's last row when calls failed; the bar words it."""


# --- the calls -----------------------------------------------------------------------------------


class FillCall(Protocol):
    """One MusicBrainz call of the fill, and the log names its answer fills (S7)."""

    @property
    def tier(self) -> FillTier: ...
    @property
    def names(self) -> frozenset[str]: ...
    @property
    def key(self) -> str: ...
    @property
    def keys(self) -> tuple[str, ...]: ...
    def ask(self, client: MusicBrainzClientProtocol) -> None: ...
    def with_names(self, names: frozenset[str]) -> FillCall: ...


@dataclass(frozen=True)
class NameTitleCall:
    """A name + title search (payoff, C68, confirmations, tail); no words is title-only (E5)."""

    tier: FillTier
    words: tuple[str, ...]
    title: str
    names: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        return name_title_key(self.words, self.title)

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key,)

    def ask(self, client: MusicBrainzClientProtocol) -> None:
        client.search_recordings_by_name_title(self.words, self.title)

    def with_names(self, names: frozenset[str]) -> NameTitleCall:
        return replace(self, names=self.names | names)


@dataclass(frozen=True)
class RecordingCall:
    """C18: a search under a manual name's one artist, with the title song matching sends."""

    mbid: str
    title: str
    names: frozenset[str] = frozenset()
    tier: FillTier = FillTier.C18

    @property
    def key(self) -> str:
        return recording_search_key(self.mbid, self.title)

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key,)

    def ask(self, client: MusicBrainzClientProtocol) -> None:
        client.search_recording(self.mbid, self.title)

    def with_names(self, names: frozenset[str]) -> RecordingCall:
        return replace(self, names=self.names | names)


@dataclass(frozen=True)
class AliasCall:
    """D3: one ``arid:`` search for up to 100 artists' aliases, then their alias names (R10),
    written in the same transaction as the cache rows (M6)."""

    mbids: tuple[str, ...]
    write: AliasWriter = field(compare=False)
    names: frozenset[str] = frozenset()
    tier: FillTier = FillTier.ALIASES

    @property
    def key(self) -> str:
        return aliases_key(self.mbids[0])

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(aliases_key(mbid) for mbid in self.mbids)

    def ask(self, client: MusicBrainzClientProtocol) -> None:
        self.write(client.search_artist_aliases_by_mbids(self.mbids))

    def with_names(self, names: frozenset[str]) -> AliasCall:
        return self


def work_link_key(recording_mbid: str) -> str:
    """The cache key of a recording's work link, as the fill fetched it (PR 3, R3-e)."""
    return f"recording-work:{recording_mbid}"


@dataclass(frozen=True)
class WorkLinkCall:
    """PR 3 (S5, R3-d): one release lookup through ``lookup_release_for_links``, for the
    recordings the work list planned it for. Its keys are theirs, so a refused or skipped
    release leaves them in the run's missed keys; its key is the release's. A 404 writes the
    client's ``release-missing:`` marker and nothing else (R3-f)."""

    release_mbid: str
    recordings: tuple[str, ...]
    write: WorkLinkWriter = field(compare=False)
    names: frozenset[str] = frozenset()  # the tier fills no names (R3-h)
    tier: FillTier = FillTier.WORK_LINKS

    @property
    def key(self) -> str:
        return f"release:{self.release_mbid}"

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(work_link_key(recording) for recording in self.recordings)

    def ask(self, client: MusicBrainzClientProtocol) -> None:
        release = client.lookup_release_for_links(self.release_mbid)
        if release is not None:
            self.write(release)

    def with_names(self, names: frozenset[str]) -> WorkLinkCall:
        return self


def work_links_of(release: MbRelease) -> dict[str, dict[str, Any]]:
    """R3-e: each track's recording on every medium, with its ``recording-work:`` row: the work
    of its first ``performance`` relation naming a work with an id and a title, or none."""
    rows: dict[str, dict[str, Any]] = {}
    for medium in release.get("media", []):
        for track in medium.get("tracks", []):
            recording = track.get("recording", {})
            recording_mbid = recording.get("id")
            if not recording_mbid or recording_mbid in rows:
                continue
            work = performed_work_of(recording)
            rows[recording_mbid] = {
                "work": None if work is None else {"id": work[0], "title": work[1]}
            }
    return rows


def work_link_writer(cache: MusicBrainzCacheRepository, ttl_days: int) -> WorkLinkWriter:
    """Write a looked-up release's ``recording-work:`` rows at the client's TTL, on the batch's
    connection, so they commit with the call (R8)."""

    def write(release: MbRelease) -> None:
        cached_at = datetime.now(UTC)
        cache.set_many(
            [
                MusicBrainzCache(
                    id=uuid4(),
                    cache_key=work_link_key(recording_mbid),
                    entity_type="recording-work",
                    entity_mbid=recording_mbid,
                    response_data=row,
                    cached_at=cached_at,
                    expires_at=cached_at + timedelta(days=ttl_days),
                )
                for recording_mbid, row in work_links_of(release).items()
            ]
        )

    return write


def alias_writer(artists: ArtistCatalogRepository, names: ArtistNameRepository) -> AliasWriter:
    """Write each alias as a ``musicbrainz_alias`` name of the artist holding its MBID."""

    def write(found: Mapping[str, list[MbAlias]]) -> None:
        holders = artists.artist_ids_by_mbid(list(found))
        for mbid, aliases in found.items():
            artist_id = holders.get(mbid)
            if artist_id is None:
                continue
            for alias in aliases:
                name = (alias.get("name") or "").strip()
                normalized = normalize_artist(name) if name else ""
                if normalized:
                    names.add(artist_id, name, normalized, ArtistNameSource.MUSICBRAINZ_ALIAS)

    return write


# --- planning --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FillSources:
    """What planning reads, and the writers of the alias step and the work-link tier."""

    work: CacheFillWorkRepository
    cache: MusicBrainzCacheRepository
    write_aliases: AliasWriter
    write_work_links: WorkLinkWriter
    name_max_len: int  # the log's name field: a name that fills it may be cut off


def plan_run(sources: FillSources, run: FillRun, limits: FillLimits) -> list[FillCall]:
    """Every call the run still has room for, in tier order (a query: it calls nothing)."""
    by_tier = _search_calls(sources)
    mbids = sources.work.catalog_mbids()
    wanted = [key for calls in by_tier.values() for call in calls for key in call.keys]
    done = sources.cache.live_keys([*wanted, *map(aliases_key, mbids)]) | run.missed
    by_tier[FillTier.WORK_LINKS] = _work_link_calls(
        sources.work.unlinked_recordings(), done, sources.write_work_links
    )
    by_tier[FillTier.ALIASES] = _alias_calls(mbids, done, sources.write_aliases)
    return _within_caps(by_tier, done, run, limits)


def _search_calls(sources: FillSources) -> dict[FillTier, list[FillCall]]:
    pairs = sources.work.searched_pairs(ARTIST_UNDECIDED_STATUSES)
    payoff = [p for p in pairs if p.in_library and not p.common]
    tail = [p for p in pairs if p.in_library and p.common] + [p for p in pairs if not p.in_library]
    max_len = sources.name_max_len
    return {
        FillTier.PAYOFF: _pair_calls(FillTier.PAYOFF, payoff, max_len),
        FillTier.C68: _file_calls(sources.work.untagged_files()),
        FillTier.C18: _manual_calls(sources.work.manual_titles()),
        FillTier.CONFIRMATIONS: _queued_calls(sources.work.confirmations(), max_len),
        FillTier.TAIL: _pair_calls(FillTier.TAIL, tail, max_len),
    }


@dataclass(frozen=True)
class _Searched:
    """A log name with one of its titles, as a name + title search reads it."""

    original_name: str
    normalized_name: str
    title: str


def _name_title(tier: FillTier, searched: _Searched, max_len: int) -> NameTitleCall | None:
    """The log name's search, or None when the title leaves nothing to search. The title keeps
    MusicBrainz's spelling and drops only the log's f/ credit ("is also the form to search
    MusicBrainz with", ``broadcast_title_variants``)."""
    phrase = search_title(credit_stripped_title(searched.title))
    if not phrase:
        return None
    name = searched.original_name
    words = name_words(name, truncated=is_likely_truncated(name, max_len))
    return NameTitleCall(tier, words, phrase, frozenset({searched.normalized_name}))


def _pair_calls(tier: FillTier, pairs: Sequence[NamePair], max_len: int) -> list[FillCall]:
    searched = (_Searched(p.original_name, p.normalized_name, p.title) for p in pairs)
    calls = (_name_title(tier, s, max_len) for s in searched)
    return [call for call in calls if call is not None]


def _queued_calls(queued: Sequence[QueuedPair], max_len: int) -> list[FillCall]:
    searched = (_Searched(q.original_name, q.normalized_name, q.title) for q in queued)
    calls = (_name_title(FillTier.CONFIRMATIONS, s, max_len) for s in searched)
    return [call for call in calls if call is not None]


def _file_calls(files: Sequence[UntaggedFile]) -> list[FillCall]:
    """C68: one search per key, the files of every spelling of it counted together."""
    counted: dict[str, tuple[NameTitleCall, int]] = {}
    for file in files:
        phrase = search_title(file.title)
        if not phrase or not has_joiner(file.artist_name):
            continue
        call = NameTitleCall(FillTier.C68, name_words(file.artist_name, truncated=False), phrase)
        first, files_before = counted.get(call.key, (call, 0))
        counted[call.key] = (first, files_before + file.files)
    ranked = sorted(counted.values(), key=lambda pair: (-pair[1], pair[0].key))
    return [call for call, _files in ranked]


def _manual_calls(titles: Sequence[ManualTitle]) -> list[FillCall]:
    calls: list[FillCall] = []
    for title in titles:
        variants = broadcast_title_variants(title.title)
        if not variants or not variants[0] or not has_joiner(title.original_name):
            continue
        calls.append(
            RecordingCall(title.artist_mbid, variants[0], frozenset({title.normalized_name}))
        )
    return calls


def _alias_calls(mbids: Sequence[str], done: Collection[str], write: AliasWriter) -> list[FillCall]:
    wanted = [mbid for mbid in mbids if aliases_key(mbid) not in done]
    return [
        AliasCall(tuple(wanted[start : start + ALIASES_PER_CALL]), write)
        for start in range(0, len(wanted), ALIASES_PER_CALL)
    ]


def _work_link_calls(
    found: Iterable[UnlinkedRecording], done: Collection[str], write: WorkLinkWriter
) -> list[FillCall]:
    """R3-c: the work list's releases, the one covering the most recordings first, ties by its
    MBID, each later one counting only the recordings still left, so some are never asked. A
    recording whose key the run already missed (refused or skipped) waits for the next run: it
    is left out before any release is picked (D16, D10)."""
    carried: dict[str, set[str]] = defaultdict(set)
    for item in found:
        if work_link_key(item.recording_mbid) not in done:
            carried[item.release_mbid].add(item.recording_mbid)
    return [
        WorkLinkCall(release, tuple(sorted(recordings)), write)
        for release, recordings in _most_covering_first(carried)
    ]


def _most_covering_first(
    carried: Mapping[str, set[str]],
) -> list[tuple[str, frozenset[str]]]:
    """Greedy cover, lazily: a release's count only falls as others are picked, so one popped
    whose count still holds is the most covering left (ties by MBID, the heap's second key)."""
    heap = [(-len(recordings), release) for release, recordings in carried.items()]
    heapq.heapify(heap)
    covered: set[str] = set()
    picked: list[tuple[str, frozenset[str]]] = []
    while heap:
        count, release = heapq.heappop(heap)
        left = carried[release] - covered
        if not left:
            continue
        if len(left) < -count:
            heapq.heappush(heap, (-len(left), release))
            continue
        picked.append((release, frozenset(left)))
        covered |= left
    return picked


def _within_caps(
    by_tier: Mapping[FillTier, Sequence[FillCall]],
    done: Collection[str],
    run: FillRun,
    limits: FillLimits,
) -> list[FillCall]:
    """Each tier's calls not done, up to its room left this run; a key already planned takes
    the later call's names instead of a second call (M9), even past the tier's room, so every
    name the key fills is re-checked (S7)."""
    planned: dict[str, FillCall] = {}
    for tier in FillTier:
        cap = limits.cap(tier)
        room = None if cap is None else max(0, cap - run.used(tier))
        taken = 0
        for call in by_tier.get(tier, ()):
            if any(key in done for key in call.keys):
                continue
            if call.key in planned:
                planned[call.key] = planned[call.key].with_names(call.names)
            elif room is None or taken < room:
                planned[call.key] = call
                taken += 1
    return list(planned.values())


# --- one batch -----------------------------------------------------------------------------------


class _Answer(Enum):
    ANSWERED = auto()
    FAILED = auto()  # transient: counts toward the cut-off (E1)
    SKIPPED = auto()  # permanent: a 4xx other than 429 (M5)


@dataclass(frozen=True)
class BatchPorts:
    client: MusicBrainzClientProtocol
    commit: Callable[[], None]
    clock: Callable[[], float]  # monotonic seconds: the batch's 110 s, the pause included
    report: Callable[[dict[str, Any]], None]  # the batch's RUNNING row
    sleep: Callable[[float], None]  # the pause after a refusal (D10)
    now: Callable[[], datetime]  # the wall clock: the run's minutes since started_at (D11)
    requests: Callable[[], int]  # the client's requests so far, every attempt (D9)


@dataclass(frozen=True)
class BatchOutcome:
    """What a batch leaves: the run to hand on (None when the run ends), the re-checks to queue
    (tier, sorted normalized names), and the batch's last row."""

    next_run: FillRun | None
    rechecks: list[tuple[FillTier, list[str]]]
    progress: dict[str, Any]


@dataclass
class _Tally:
    """The run's counts as this batch adds to them."""

    calls: dict[FillTier, int]
    failed: dict[FillTier, int]
    skipped: dict[FillTier, int]
    missed: set[str]
    in_a_row: int
    refused_in_a_row: int
    filled: dict[FillTier, set[str]]
    requests: int

    @classmethod
    def of(cls, run: FillRun) -> _Tally:
        return cls(
            calls=dict(run.calls),
            failed=dict(run.failed),
            skipped=dict(run.skipped),
            missed=set(run.missed),
            in_a_row=run.in_a_row,
            refused_in_a_row=run.refused_in_a_row,
            filled={tier: set(names) for tier, names in run.filled.items()},
            requests=run.requests,
        )

    def record(self, call: FillCall, answer: _Answer) -> None:
        tier = call.tier
        self.calls[tier] = self.calls.get(tier, 0) + 1
        if answer is _Answer.ANSWERED:
            self.in_a_row = 0
            self.refused_in_a_row = 0
            self.filled.setdefault(tier, set()).update(call.names)
            return
        self.missed.update(call.keys)
        if answer is _Answer.FAILED:
            self.failed[tier] = self.failed.get(tier, 0) + 1
            self.in_a_row += 1
        else:
            self.skipped[tier] = self.skipped.get(tier, 0) + 1
            self.refused_in_a_row += 1

    def run(self, started_at: str) -> FillRun:
        return FillRun(
            started_at=started_at,
            calls=dict(self.calls),
            failed=dict(self.failed),
            skipped=dict(self.skipped),
            missed=frozenset(self.missed),
            in_a_row=self.in_a_row,
            refused_in_a_row=self.refused_in_a_row,
            filled={tier: frozenset(names) for tier, names in self.filled.items() if names},
            requests=self.requests,
        )


def _ask(call: FillCall, client: MusicBrainzClientProtocol) -> _Answer:
    """Make the call. Any other failure (a 200 that isn't JSON, a proxy or protocol error) is
    not MusicBrainz being away, so it fails the batch, as the song path treats it."""
    try:
        call.ask(client)
    except httpx.HTTPError as exc:
        if is_transient_mb_failure(exc):
            logger.warning(
                "mb_cache_fill_call_failed", tier=call.tier, key=call.key, error=str(exc)
            )
            return _Answer.FAILED
        if not isinstance(exc, httpx.HTTPStatusError):
            raise
        logger.warning(
            "mb_cache_fill_item_skipped",
            tier=call.tier,
            key=call.key,
            status=exc.response.status_code,
        )
        return _Answer.SKIPPED
    return _Answer.ANSWERED


def _elapsed(started_at: str, now: datetime) -> timedelta:
    """The run's wall-clock time so far (D11): from its ``started_at`` to *now*."""
    return now - datetime.fromisoformat(started_at)


def _time_up(started_at: str, run_minutes: float, now: datetime) -> bool:
    """D11: the run's minutes are spent, so it hands on no batch."""
    return _elapsed(started_at, now) >= timedelta(minutes=run_minutes)


def _ended(*, goes_on: bool, cut_off: bool, work_left: bool) -> FillEnd | None:
    """How the run ended, or None while it hands on (D10's 4b): a cut-off first, even as the
    time runs out (D8's words say more), then the time limit with work left (D11, or a refusal
    whose pause the run's minutes end inside, D19), else nothing was left to do."""
    if goes_on:
        return None
    if cut_off:
        return FillEnd.CUT_OFF
    return FillEnd.TIME if work_left else FillEnd.DONE


@dataclass
class _Batch:
    """One batch as it goes: the run's plan, the batch's limits, and the calls made so far."""

    plan: Sequence[FillCall]
    limits: FillLimits
    started_at: str
    tally: _Tally
    made: list[FillCall] = field(default_factory=list)
    ended_on_refusal: bool = False  # its last call a refusal no request follows (D18, D19)

    @property
    def calls(self) -> Sequence[FillCall]:
        """The calls this batch may make: the plan's first ``batch_calls``."""
        return self.plan[: self.limits.batch_calls]

    @property
    def cut_off(self) -> bool:
        return self.tally.in_a_row >= self.limits.cutoff

    @property
    def work_left(self) -> bool:
        """The run has planned calls this batch hasn't made (this batch's or a later one's)."""
        return len(self.plan) > len(self.made)

    def request_follows(self, elapsed: float, now: datetime) -> bool:
        """After a refusal, another request follows in the run once the pause is over (R2b-b):
        this batch makes another call, or it ends and the run, its minutes not yet spent, hands
        on a batch. When none follows, the batch and the run end at the refusal (D18, D19).
        *elapsed* is the batch's seconds so far; *now* the wall clock."""
        if not self.work_left:
            return False
        pause = self.limits.pause_seconds
        if len(self.made) < len(self.calls) and elapsed + pause < self.limits.batch_seconds:
            return True
        after_pause = now + timedelta(seconds=pause)
        return not _time_up(self.started_at, self.limits.run_minutes, after_pause)

    def detail(self, tier: FillTier) -> str:
        """D4: "payoff, 37 of 100 (cap 2,600)"; the uncapped alias step names no cap. Once the
        tier has failed calls in the run, D10 counts them: "payoff, 37 of 100 (cap 2,600), 12
        failed"."""
        cap = self.limits.cap(tier)
        text = f"{tier.value}, {len(self.made)} of {len(self.calls)}"
        text = text if cap is None else f"{text} (cap {cap:,})"
        failed = self.tally.failed.get(tier, 0)
        return f"{text}, {failed} failed" if failed else text

    def row(self, tier: FillTier | None) -> dict[str, Any]:
        """The batch's progress row, the next (or last) call's tier named."""
        return {
            "tier": None if tier is None else tier.value,
            "processed": len(self.made),
            "total": len(self.calls),
            "cap": None if tier is None else self.limits.cap(tier),
            "detail": "nothing to fill" if tier is None else self.detail(tier),
            "run_started_at": self.started_at,
            **self.tally.run(self.started_at).totals(),
            "requests": self.tally.requests,
        }

    def last_row(self, ended: FillEnd | None, now: datetime) -> dict[str, Any]:
        """The batch's last row: the last call's tier, or for a run the time limit ended the
        tier the rest waits in (D12); the run's minutes and how it ended; and on the run's last
        row, its failed calls when there are any (D10's 4b)."""
        last = self.made[-1] if self.made else None
        if ended is FillEnd.TIME:
            waiting = self.plan[len(self.made)].tier
            row = self.row(waiting)
            row["detail"] = f"{waiting.value}, {TIME_UP}"
        else:
            row = self.row(None if last is None else last.tier)
            if ended is FillEnd.CUT_OFF and last is not None:
                row["detail"] = f"{last.tier.value}, {STOPPED}"
        minutes = round(_elapsed(self.started_at, now).total_seconds() / 60, 1)
        row.update(ended=ended, minutes=minutes)
        failed = sum(self.tally.failed.values())
        if ended is not None and failed:
            row.update(warning=CALLS_FAILED, failed_calls=failed)
        return row

    def outcome(self, now: datetime) -> BatchOutcome:
        """The run goes on while work is left, short of a cut-off and the run's minutes (D11).
        A batch that ended on a refusal no request follows hands on no batch whatever the clock
        reads: its pause would have spent the minutes (D19)."""
        time_up = self.ended_on_refusal or _time_up(self.started_at, self.limits.run_minutes, now)
        goes_on = self.work_left and not self.cut_off and not time_up
        ended = _ended(goes_on=goes_on, cut_off=self.cut_off, work_left=self.work_left)
        after = self.tally.run(self.started_at)
        left = set(FillTier) if not goes_on else _tiers_left(self.plan, self.made)
        rechecks = [
            (tier, sorted(after.filled[tier]))
            for tier in FillTier
            if after.filled.get(tier) and tier in left
        ]
        row = self.last_row(ended, now)
        row.update(cut_off=self.cut_off, next_batch=goes_on)
        next_run = after.handed_off(tier for tier, _names in rechecks) if goes_on else None
        return BatchOutcome(next_run=next_run, rechecks=rechecks, progress=row)


def run_batch(
    plan: Sequence[FillCall], run: FillRun, limits: FillLimits, ports: BatchPorts
) -> BatchOutcome:
    """Make the plan's first calls, at most ``batch_calls`` and none once ``batch_seconds`` have
    passed, until the run's transient failures in a row reach the cut-off. A batch that starts
    once the run's minutes are spent (after a worker restart, E6) makes no call (D11). After a
    refusal another request follows only after ``pause_seconds``, on the batch's clock (D10); a
    refusal no request follows ends the batch and the run there, the run's last call (D18, D19).

    Raises:
        MusicBrainzRefusingError: ``REFUSALS_TO_FAIL`` permanent errors in a row, with no answer
            between; the calls already made stay committed.
    """
    batch = _Batch(plan, limits, run.started_at, _Tally.of(run))
    if _time_up(run.started_at, limits.run_minutes, ports.now()):
        return batch.outcome(ports.now())
    sent_before, start = ports.requests(), ports.clock()
    for call in batch.calls:
        if ports.clock() - start >= limits.batch_seconds:
            break
        ports.report(batch.row(call.tier))
        answer = _ask(call, ports.client)
        batch.tally.record(call, answer)
        batch.tally.requests = run.requests + ports.requests() - sent_before
        ports.commit()
        batch.made.append(call)
        if batch.tally.refused_in_a_row >= REFUSALS_TO_FAIL:
            raise MusicBrainzRefusingError(_refusing(call, batch.tally))
        if batch.cut_off:
            break
        if answer is _Answer.FAILED:
            if not batch.request_follows(ports.clock() - start, ports.now()):
                batch.ended_on_refusal = True
                break
            ports.sleep(limits.pause_seconds)
    return batch.outcome(ports.now())


def _refusing(call: FillCall, tally: _Tally) -> str:
    """The FAILED batch's error: its row keeps no report of its own, so the run's requests and
    failed calls ride in the message (D10's 4b)."""
    return (
        f"MusicBrainz refused {tally.refused_in_a_row} fill calls in a row (the last "
        f"{call.key}); the run stopped after {tally.requests:,} requests, "
        f"{sum(tally.failed.values()):,} failed calls"
    )


def _tiers_left(plan: Iterable[FillCall], made: Sequence[FillCall]) -> set[FillTier]:
    """The tiers whose every planned call this batch made: the run leaves them now."""
    made_keys = {call.key for call in made}
    waiting = {call.tier for call in plan if call.key not in made_keys}
    return set(FillTier) - waiting
