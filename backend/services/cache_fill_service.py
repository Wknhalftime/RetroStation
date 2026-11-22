"""The MusicBrainz cache fill: which calls a run still owes, and one batch of them (phase 4.1
PR 2; SPEC S3, S4, S7, E1, E2, E4, E5; D3, D4, D8; M1-M11 as the coordinator read them;
AUD-R091).

``plan_run`` is a query: every call the run still has room for, in tier order (payoff, C68, C18,
confirmations, tail, then the alias step), skipping any query with a live cache entry (C42) or
tried and not answered earlier in the run, one call per cache key (M9), each tier within its cap
for the run. ``run_batch`` makes the first of them, one at a time, committing each call's rows
as they land (R8), and returns the run's next state: what to re-check, and whether the run goes
on.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Any, Protocol

import httpx
import structlog

from backend.domain.cache_fill import FillRun, ManualTitle, NamePair, QueuedPair, UntaggedFile
from backend.domain.enums import ArtistNameSource, FillTier
from backend.domain.matching import ARTIST_UNDECIDED_STATUSES
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_names import ArtistNameRepository
from backend.repositories.cache_fill_work import CacheFillWorkRepository
from backend.repositories.musicbrainz_cache import MusicBrainzCacheRepository
from backend.services.cache_fill_constants import ALIASES_PER_CALL, FillLimits
from backend.services.cache_fill_words import has_joiner, name_words, search_title
from backend.services.matching_utils import is_likely_truncated
from backend.services.mb_client import (
    MusicBrainzClientProtocol,
    aliases_key,
    name_title_key,
    recording_search_key,
)
from backend.services.mb_types import MbAlias
from backend.services.normalization import normalize_artist
from backend.services.recording_search import is_transient_mb_failure
from backend.services.title_scoring import broadcast_title_variants

logger = structlog.get_logger()

type AliasWriter = Callable[[Mapping[str, list[MbAlias]]], None]

STOPPED = "stopped (MusicBrainz not answering)"
"""D8: how a cut-off run's last row reads, after its tier."""


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
    """What planning reads, and the alias step's writer."""

    work: CacheFillWorkRepository
    cache: MusicBrainzCacheRepository
    write_aliases: AliasWriter
    name_max_len: int  # the log's name field: a name that fills it may be cut off


def plan_run(sources: FillSources, run: FillRun, limits: FillLimits) -> list[FillCall]:
    """Every call the run still has room for, in tier order (a query: it calls nothing)."""
    by_tier = _search_calls(sources)
    mbids = sources.work.catalog_mbids()
    wanted = [key for calls in by_tier.values() for call in calls for key in call.keys]
    live = sources.cache.get_many([*wanted, *map(aliases_key, mbids)]).keys()
    done = set(live) | run.missed
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


def _name_title(
    tier: FillTier, original_name: str, title: str, names: frozenset[str], max_len: int
) -> NameTitleCall | None:
    """The log name's search, or None when the title leaves nothing to search."""
    phrase = search_title(title)
    if not phrase:
        return None
    truncated = is_likely_truncated(original_name, max_len)
    return NameTitleCall(tier, name_words(original_name, truncated=truncated), phrase, names)


def _pair_calls(tier: FillTier, pairs: Sequence[NamePair], max_len: int) -> list[FillCall]:
    calls = (
        _name_title(tier, p.original_name, p.title, frozenset({p.normalized_name}), max_len)
        for p in pairs
    )
    return [call for call in calls if call is not None]


def _queued_calls(queued: Sequence[QueuedPair], max_len: int) -> list[FillCall]:
    calls = (
        _name_title(
            FillTier.CONFIRMATIONS,
            q.original_name,
            q.title,
            frozenset({q.normalized_name}),
            max_len,
        )
        for q in queued
    )
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


def _within_caps(
    by_tier: Mapping[FillTier, Sequence[FillCall]],
    done: Collection[str],
    run: FillRun,
    limits: FillLimits,
) -> list[FillCall]:
    """Each tier's calls not done, up to its room left this run; a key planned in an earlier
    tier takes the later call's names instead of a second call (M9)."""
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
                continue
            if room is not None and taken >= room:
                break
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
    clock: Callable[[], float]  # monotonic seconds
    report: Callable[[dict[str, Any]], None]  # the batch's RUNNING row


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
    filled: dict[FillTier, set[str]]

    @classmethod
    def of(cls, run: FillRun) -> _Tally:
        return cls(
            calls=dict(run.calls),
            failed=dict(run.failed),
            skipped=dict(run.skipped),
            missed=set(run.missed),
            in_a_row=run.in_a_row,
            filled={tier: set(names) for tier, names in run.filled.items()},
        )

    def record(self, call: FillCall, answer: _Answer) -> None:
        tier = call.tier
        self.calls[tier] = self.calls.get(tier, 0) + 1
        if answer is _Answer.ANSWERED:
            self.in_a_row = 0
            self.filled.setdefault(tier, set()).update(call.names)
            return
        self.missed.update(call.keys)
        if answer is _Answer.FAILED:
            self.failed[tier] = self.failed.get(tier, 0) + 1
            self.in_a_row += 1
        else:
            self.skipped[tier] = self.skipped.get(tier, 0) + 1

    def run(self, started_at: str) -> FillRun:
        return FillRun(
            started_at=started_at,
            calls=dict(self.calls),
            failed=dict(self.failed),
            skipped=dict(self.skipped),
            missed=frozenset(self.missed),
            in_a_row=self.in_a_row,
            filled={tier: frozenset(names) for tier, names in self.filled.items() if names},
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


def _detail(tier: FillTier, done: int, total: int, limits: FillLimits) -> str:
    """D4: "payoff, 37 of 100 (cap 2,600)"; the uncapped alias step names no cap."""
    cap = limits.cap(tier)
    text = f"{tier.value}, {done} of {total}"
    return text if cap is None else f"{text} (cap {cap:,})"


def _row(
    tier: FillTier | None, done: int, total: int, limits: FillLimits, run: FillRun
) -> dict[str, Any]:
    return {
        "tier": None if tier is None else tier.value,
        "processed": done,
        "total": total,
        "cap": None if tier is None else limits.cap(tier),
        "detail": "nothing to fill" if tier is None else _detail(tier, done, total, limits),
        "run_started_at": run.started_at,
        **run.totals(),
    }


def run_batch(
    plan: Sequence[FillCall], run: FillRun, limits: FillLimits, ports: BatchPorts
) -> BatchOutcome:
    """Make the plan's first calls, at most ``batch_calls`` and none once ``batch_seconds`` have
    passed, until the run's transient failures in a row reach the cut-off."""
    batch = plan[: limits.batch_calls]
    tally = _Tally.of(run)
    start = ports.clock()
    done = 0
    last: FillCall | None = None
    for call in batch:
        if ports.clock() - start >= limits.batch_seconds:
            break
        ports.report(_row(call.tier, done, len(batch), limits, tally.run(run.started_at)))
        tally.record(call, _ask(call, ports.client))
        ports.commit()
        done += 1
        last = call
        if tally.in_a_row >= limits.cutoff:
            break
    return _outcome(plan, batch[:done], last, tally, run.started_at, limits)


def _outcome(
    plan: Sequence[FillCall],
    made: Sequence[FillCall],
    last: FillCall | None,
    tally: _Tally,
    started_at: str,
    limits: FillLimits,
) -> BatchOutcome:
    cut_off = tally.in_a_row >= limits.cutoff
    goes_on = not cut_off and len(plan) > len(made)
    after = tally.run(started_at)
    left = set(FillTier) if not goes_on else _tiers_left(plan, made)
    rechecks = [
        (tier, sorted(after.filled[tier]))
        for tier in FillTier
        if after.filled.get(tier) and tier in left
    ]
    row = _row(
        None if last is None else last.tier,
        len(made),
        min(len(plan), limits.batch_calls),
        limits,
        after,
    )
    if cut_off and last is not None:
        row["detail"] = f"{last.tier.value}, {STOPPED}"
    row.update(cut_off=cut_off, next_batch=goes_on)
    next_run = after.handed_off(tier for tier, _names in rechecks) if goes_on else None
    return BatchOutcome(next_run=next_run, rechecks=rechecks, progress=row)


def _tiers_left(plan: Iterable[FillCall], made: Sequence[FillCall]) -> set[FillTier]:
    """The tiers whose every planned call this batch made: the run leaves them now."""
    made_keys = {call.key for call in made}
    waiting = {call.tier for call in plan if call.key not in made_keys}
    return set(FillTier) - waiting
