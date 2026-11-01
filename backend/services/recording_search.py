"""Song matching's MusicBrainz recording search for one run, with the outage cut-off.

AUD-R028 (D16). MusicBrainzApiClient already retries HTTP 429 and 5xx (three attempts, with
backoff) before it raises httpx.HTTPStatusError; it raises timeouts and network errors at once.
RecordingSearch translates the transient failures into the matching domain's
MusicBrainzUnavailableError, so song matching defers the song instead of failing the playlist.
After SONG_MB_FAILURE_CUTOFF of them in a row it stops calling MusicBrainz for the rest of the
run. Permanent errors propagate unchanged.
"""

from __future__ import annotations

import httpx
import structlog

from backend.domain.matching import MusicBrainzUnavailableError
from backend.services.matching_constants import SONG_MB_FAILURE_CUTOFF
from backend.services.mb_client import MusicBrainzClientProtocol, _is_transient_mb_error
from backend.services.mb_types import MbRecording

logger = structlog.get_logger()

# Network-level failures MusicBrainz may not repeat. httpx's other transport errors
# (UnsupportedProtocol, LocalProtocolError, ProxyError) are configuration faults: they propagate.
_TRANSIENT_TRANSPORT_ERRORS: tuple[type[httpx.TransportError], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


def is_transient_mb_failure(exc: httpx.HTTPError) -> bool:
    """HTTP 429 or 5xx (as the client's own retry decides), a timeout, a network error or a
    dropped connection."""
    return _is_transient_mb_error(exc) or isinstance(exc, _TRANSIENT_TRANSPORT_ERRORS)


class RecordingSearch:
    """MusicBrainz recording search for one run of song matching (AUD-R028, D16).

    build_song_engines builds one per run, so the failure count lives on this
    instance, never in module state: each playlist run starts at zero. A successful search,
    cached or live, resets it.

    With ``remember_answers`` (trial mode, AUD-R100) a search repeated in the run returns the
    first one's answer without calling the client, so both of a song's scoring chains use one
    MusicBrainz search. A failure is not remembered.
    """

    def __init__(
        self,
        client: MusicBrainzClientProtocol,
        cutoff: int = SONG_MB_FAILURE_CUTOFF,
        *,
        remember_answers: bool = False,
    ) -> None:
        self._client = client
        self._cutoff = cutoff
        self._failures = 0
        self._answers: dict[tuple[str, str], list[MbRecording]] | None = (
            {} if remember_answers else None
        )

    @property
    def cut_off(self) -> bool:
        """True once this run has stopped calling MusicBrainz."""
        return self._failures >= self._cutoff

    def search(self, artist_mbid: str, title: str) -> list[MbRecording]:
        """The client's search_recording, guarded by the cut-off.

        Raises:
            MusicBrainzUnavailableError: the search failed transiently, or the cut-off was
                reached earlier in this run (MusicBrainz is then not called).
            httpx.HTTPError: a permanent failure (another 4xx, a configuration fault).
        """
        if self.cut_off:
            raise MusicBrainzUnavailableError("MusicBrainz cut-off reached in this run")
        if self._answers is not None and (artist_mbid, title) in self._answers:
            return self._answers[(artist_mbid, title)]
        try:
            recordings = self._client.search_recording(artist_mbid=artist_mbid, title=title)
        except httpx.HTTPError as exc:
            if not is_transient_mb_failure(exc):
                raise
            self._failures += 1
            logger.warning(
                "song_matching_mb_search_failed",
                artist_mbid=artist_mbid,
                title=title,
                consecutive_failures=self._failures,
                error=str(exc),
            )
            if self.cut_off:
                logger.warning("song_matching_mb_cut_off", consecutive_failures=self._failures)
            raise MusicBrainzUnavailableError(str(exc)) from exc
        self._failures = 0
        if self._answers is not None:
            self._answers[(artist_mbid, title)] = recordings
        return recordings
