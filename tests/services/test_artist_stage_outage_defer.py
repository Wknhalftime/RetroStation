"""The artist stage defers a cut-off name when MusicBrainz is down (MusicBrainz cache fill, phase
4.1 PR 1; SPEC S6, D2; AUD-R028 extended by AUD-R042, AUD-R090).

Until the old artist stage retires (4.3), it asks MusicBrainz only about names that look cut off
by the log's fixed-width field. A transient failure there (429 or 5xx after the client's own
retries, a timeout, a network error or a dropped connection, as
``recording_search.is_transient_mb_failure`` reads it) used to fail the whole run. Now
``DeferOnMusicBrainzOutage``, which ``build_mb_artist_engine`` wraps around the MusicBrainz
strategy inside the cut-off gate, answers such a name as the stage answers any name it can't
resolve: ``needs_review`` / ``DEFERRED_RETRY``. A permanent error (400, 404) still fails the run,
as AUD-R028 has it for songs, and the gate's dry run still sees its cache miss
(``MusicBrainzUnavailableError``). After ``ARTIST_MB_FAILURE_CUTOFF`` (5) transient failures in a
row in one run, the remaining cut-off names defer without a call; any answer resets the count.
Each engine is one run (one playlist), so the count is never shared.

The locked ``test_artist_stage_mb_outage.py`` pins the raw strategies still raising and the
coalesced search's return; this file pins the wrapper.
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from backend.domain.broadcast import BroadcastArtist
from backend.domain.enums import MatchStatus, MatchTier, ReasonCode
from backend.domain.matching import MusicBrainzUnavailableError
from backend.services.artist_matching_service import (
    ArtistEngineInputs,
    ArtistMatchResult,
    ArtistMatchThresholds,
    DeferOnMusicBrainzOutage,
    DeferredRetryStrategy,
    build_mb_artist_engine,
)
from backend.services.matching_constants import ARTIST_MB_FAILURE_CUTOFF
from backend.services.matching_utils import is_likely_truncated
from backend.services.normalization import normalize_artist

MAX_LEN = 30
CUT_OFF = "QUIXOTIC VELVET HARMONY ORCHES"
WHOLE = "KNOWN BAND"


def _name(original: str) -> BroadcastArtist:
    return BroadcastArtist(
        id=uuid4(), original_name=original, normalized_name=normalize_artist(original)
    )


def _cut_off(n: int) -> BroadcastArtist:
    """A distinct name that looks cut off (30 characters, ending mid-word)."""
    original = f"QUIXOTIC VELVET HARMONY ORCH{n:02d}"
    assert is_likely_truncated(original, MAX_LEN)
    return _name(original)


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://musicbrainz.org/ws/2/artist/")
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request)
    )


def _request() -> httpx.Request:
    return httpx.Request("GET", "https://musicbrainz.org/ws/2/artist/")


TRANSIENT: dict[str, BaseException] = {
    "503": _status_error(503),
    "500": _status_error(500),
    "429": _status_error(429),
    "timeout": httpx.ReadTimeout("timed out", request=_request()),
    "network": httpx.ConnectError("refused", request=_request()),
    "dropped": httpx.RemoteProtocolError("closed", request=_request()),
}

PERMANENT: dict[str, BaseException] = {
    "400": _status_error(400),
    "404": _status_error(404),
    "unsupported-protocol": httpx.UnsupportedProtocol("ftp", request=_request()),
}

ANSWER = ArtistMatchResult(
    status=MatchStatus.AUTO_MATCHED,
    tier=MatchTier.MUSICBRAINZ_API,
    confidence_score=100.0,
    target_id="mbid-of-the-act",
)


class _Inner:
    """A strategy that answers from a script: an exception to raise, a result, or None."""

    def __init__(self, *script: BaseException | ArtistMatchResult | None) -> None:
        self.script = list(script)
        self.calls: list[str] = []

    def apply(self, broadcast_artist: BroadcastArtist) -> ArtistMatchResult | None:
        self.calls.append(broadcast_artist.original_name)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, BaseException):
            raise step
        return step


def _deferred(name: BroadcastArtist) -> ArtistMatchResult | None:
    return DeferredRetryStrategy().apply(name)


# --- one name --------------------------------------------------------------------------------


@pytest.mark.parametrize("error", list(TRANSIENT.values()), ids=list(TRANSIENT))
def test_a_transient_error_defers_the_name(error: BaseException) -> None:
    name = _name(CUT_OFF)

    result = DeferOnMusicBrainzOutage(_Inner(error)).apply(name)

    assert result == _deferred(name)
    assert result is not None
    assert (result.status, result.reason_code, result.tier, result.confidence_score) == (
        MatchStatus.NEEDS_REVIEW,
        ReasonCode.DEFERRED_RETRY,
        MatchTier.UNCLASSIFIED,
        0.0,
    )


@pytest.mark.parametrize("error", list(PERMANENT.values()), ids=list(PERMANENT))
def test_a_permanent_error_still_fails_the_run(error: BaseException) -> None:
    with pytest.raises(type(error)):
        DeferOnMusicBrainzOutage(_Inner(error)).apply(_name(CUT_OFF))


def test_the_dry_runs_cache_miss_passes_through() -> None:
    """The gate's dry run marks a cut-off name "unavailable" when its cache-only client raises
    ``MusicBrainzUnavailableError``; the wrapper is for httpx's transient errors only."""
    inner = _Inner(MusicBrainzUnavailableError("not in the MusicBrainz cache"))

    with pytest.raises(MusicBrainzUnavailableError):
        DeferOnMusicBrainzOutage(inner).apply(_name(CUT_OFF))


def test_an_answer_or_no_answer_passes_through() -> None:
    assert DeferOnMusicBrainzOutage(_Inner(ANSWER)).apply(_name(CUT_OFF)) == ANSWER
    assert DeferOnMusicBrainzOutage(_Inner(None)).apply(_name(WHOLE)) is None


# --- the cut-off -----------------------------------------------------------------------------


def test_the_cutoff_is_five_as_for_songs() -> None:
    assert ARTIST_MB_FAILURE_CUTOFF == 5


def test_after_five_failures_in_a_row_the_rest_defer_without_a_call() -> None:
    inner = _Inner(TRANSIENT["503"])
    wrapper = DeferOnMusicBrainzOutage(inner)
    names = [_cut_off(n) for n in range(8)]

    results = [wrapper.apply(name) for name in names]

    assert results == [_deferred(name) for name in names]
    assert inner.calls == [name.original_name for name in names[:5]]


def test_any_answer_resets_the_count() -> None:
    """Four failures, an answer, then five failures: ten calls. The answer reset the count, so
    only the fifth failure after it cuts the run off, and the last two names make no call."""
    script: list[BaseException | ArtistMatchResult | None] = [TRANSIENT["timeout"]] * 4
    script += [None]
    script += [TRANSIENT["503"]] * 5
    inner = _Inner(*script, TRANSIENT["503"])
    wrapper = DeferOnMusicBrainzOutage(inner)
    names = [_cut_off(n) for n in range(12)]

    results = [wrapper.apply(name) for name in names]

    assert len(inner.calls) == 10
    assert results[4] is None
    assert results[10:] == [_deferred(n) for n in names[10:]]


def test_a_permanent_error_does_not_count_toward_the_cutoff() -> None:
    inner = _Inner(*([TRANSIENT["503"]] * 4), PERMANENT["404"], TRANSIENT["503"])
    wrapper = DeferOnMusicBrainzOutage(inner)
    names = [_cut_off(n) for n in range(6)]

    for name in names[:4]:
        wrapper.apply(name)
    with pytest.raises(httpx.HTTPStatusError):
        wrapper.apply(names[4])
    wrapper.apply(names[5])

    assert len(inner.calls) == 6


# --- the engine the stage builds --------------------------------------------------------------


class _MusicBrainzDown:
    """Every search fails as the real client does once its retries give up on a 503."""

    live_fetches = 0
    cache_hits = 0

    def __init__(self) -> None:
        self.calls: list[str] = []

    def search_artist(self, name: str) -> list[dict[str, Any]]:
        self.calls.append(name)
        raise _status_error(503)


def _engine(client: _MusicBrainzDown) -> Any:
    inputs = ArtistEngineInputs(
        repos=cast(Any, None),  # build_mb_artist_engine reads only the client and thresholds
        mb_client=cast(Any, client),
        thresholds=ArtistMatchThresholds(broadcast_name_max_len=MAX_LEN),
    )
    return build_mb_artist_engine(inputs, {})


def test_the_engine_defers_a_cut_off_name_and_asks_nothing_for_a_whole_one() -> None:
    client = _MusicBrainzDown()
    engine = _engine(client)
    cut_off, whole = _name(CUT_OFF), _name(WHOLE)

    assert engine.resolve(cut_off) == _deferred(cut_off)
    assert engine.resolve(whole) == _deferred(whole)
    assert client.calls == [CUT_OFF]


def test_each_engine_is_one_run_with_its_own_count() -> None:
    """The stage builds one engine per playlist run, so a run cut off doesn't cut off the next."""
    client = _MusicBrainzDown()
    first, second = _engine(client), _engine(client)

    for n in range(7):
        first.resolve(_cut_off(n))
    second.resolve(_cut_off(7))

    assert len(client.calls) == 6
    assert client.calls[-1] == _cut_off(7).original_name
