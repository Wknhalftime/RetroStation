"""Acceptance tests: a MusicBrainz outage defers songs, never fails a playlist (D16, AUD-R028).

Spec 2026-10-05 §2 D16 (Lance and the controller, 2026-10-12):
- Song matching calls MusicBrainz only in Tier 1 Step B, the recording search, which runs when
  Step A (the files tagged with the artist's MBID) did not auto-match.
- When that search fails transiently (HTTP 429 or 5xx once the client's own retries give up, a
  timeout, a network error, a dropped connection), the song is written needs_review /
  MB_UNAVAILABLE (a reason code of its own) with the "MusicBrainz unavailable" text, through
  the guarded write. Nothing is decided on partial evidence: Step A's and Step C's results are
  dropped (even a Step C auto-match), the 56% floor is not applied, no match row is written and
  the old suggestion is deleted. The rest of the playlist is decided and the run returns
  normally, so the task no longer reports FAILED.
- A permanent error (another 4xx, a configuration fault) still propagates.
- The cut-off: after 5 consecutive transient failures in one run (one call of
  match_identities_for_playlist), the run makes no more MusicBrainz calls and defers every
  remaining song that reaches Step B. A successful search resets the count. Songs that never
  reach Step B are decided as usual. Each failure and the trip are logged, and the run summary
  counts the deferrals.
- Every re-check (rewind_wave) also rewinds every needs_review / MB_UNAVAILABLE song, whatever
  its artist. DEFERRED_RETRY songs (the artist cascade) keep artist matching's own path.

Each song gets its own artist, so no file is a candidate for two songs. Candidate scores are
pinned per file by patching _candidate_scores, as test_song_floor.py does. The new names are
read as attributes (mc.SONG_MB_FAILURE_CUTOFF, reasons.format_mb_unavailable,
ReasonCode.MB_UNAVAILABLE), so before the build each test fails on its own instead of the whole
file failing to import.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from structlog.testing import capture_logs

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services import matching_constants as mc
from backend.services import matching_reasons as reasons
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.matching_recheck_service import (
    RewindCounts,
    playlists_with_pending_work,
    rewind_wave,
)
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.matching_seed import seed_link

Scores = dict[UUID, float]
_URL = "https://musicbrainz.org/ws/2/recording/"


def _http_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", _URL)
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"{code} from MusicBrainz", request=request, response=response)


def _request() -> httpx.Request:
    return httpx.Request("GET", _URL)


# What the real client raises once its own retries give up (mb_client._fetch: tenacity, three
# attempts, on 429 and 5xx only). Transport errors are raised at the first attempt.
TRANSIENT: dict[str, BaseException] = {
    "503": _http_error(503),  # observed 2026-10-10
    "500": _http_error(500),
    "502": _http_error(502),
    "429": _http_error(429),
    "connect_error": httpx.ConnectError("connection refused", request=_request()),
    "read_timeout": httpx.ReadTimeout("timed out", request=_request()),
    "dropped_connection": httpx.RemoteProtocolError("peer closed", request=_request()),
    "connect_timeout": httpx.ConnectTimeout("connect timed out", request=_request()),
    "connection_reset": httpx.ReadError("connection reset by peer", request=_request()),
}
PERMANENT: dict[str, BaseException] = {
    "400": _http_error(400),
    "404": _http_error(404),
    "unsupported_protocol": httpx.UnsupportedProtocol("no scheme", request=_request()),
    "proxy_error": httpx.ProxyError("proxy refused", request=_request()),
    "not_json": json.JSONDecodeError("Expecting value", "<html>", 0),  # a 200 with HTML
}


class _Mb(FakeMbClient):
    """FakeMbClient whose recording search raises for chosen artist MBIDs (D16).

    ``errors`` maps an artist MBID to what a search under it raises; ``down`` makes every
    search raise (a full outage); ``cached`` answers those MBIDs from the client's cache,
    counted in ``cache_hits`` as the real client does. FakeMbClient records each call
    first, so ``searches`` counts the searches the run made, failed or not.
    """

    def __init__(
        self,
        errors: dict[str, BaseException] | None = None,
        *,
        down: BaseException | None = None,
        recording_searches: dict[tuple[str, str], list[dict[str, Any]]] | None = None,
        cached: set[str] | None = None,
    ) -> None:
        super().__init__(recording_searches=recording_searches)
        self.errors = errors or {}
        self.down = down
        self.cached = cached or set()

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        found = super().search_recording(artist_mbid, title, limit)
        error = self.down or self.errors.get(artist_mbid)
        if error is not None:
            raise error
        if artist_mbid in self.cached:
            self.cache_hits += 1
        return found

    @property
    def searches(self) -> int:
        return sum(1 for call in self.calls if call.startswith("search_recording:"))


@pytest.fixture
def scores(monkeypatch: pytest.MonkeyPatch) -> Scores:
    """Pin each candidate's score by library-file id (rank score and tie-break score alike)."""
    table: Scores = {}

    def pinned(
        full_bcs: list[str], core_bcs: list[str], f: LibraryFile, threshold: int
    ) -> tuple[float, float]:
        return table[f.id], table[f.id]

    monkeypatch.setattr("backend.services.identity_matching_service._candidate_scores", pinned)
    return table


class _Matches(FakeMatchRepository):
    def rows_for(self, identity_id: UUID) -> list[Match]:
        return [m for m in self._data.values() if m.identity_id == identity_id]


class _Songs(FakeBroadcastTrackIdentityRepository):
    """Can decide a song for the user just after the worker has read it as PENDING."""

    def __init__(self) -> None:
        super().__init__()
        self.decide_after_read: dict[UUID, MatchStatus] = {}

    def get_pending_for_playlist(self, playlist_id: UUID) -> list[BroadcastTrackIdentity]:
        snapshot = super().get_pending_for_playlist(playlist_id)
        for identity_id, decision in self.decide_after_read.items():
            self.update_match_status(identity_id, decision, MatchTier.MANUAL)
        return snapshot


class _Rig:
    """One playlist; each song has its own artist.

    A Tier 1 song's artist is auto_matched with a link to the artist of its MBID (a stub, as
    no catalog artist holds it): Steps A, B and C. A Tier 2 song's artist is pending: Tier 2
    never calls MusicBrainz.
    """

    def __init__(self, scores: Scores) -> None:
        self.scores = scores
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = _Songs()
        self.matches = _Matches()
        self.links = FakeLogNameLinkRepository()
        self.catalog = FakeArtistRepository()
        self.files = FakeLibraryFileRepository()
        self._artist_of: dict[UUID, BroadcastArtist] = {}

    def song(
        self,
        title: str = "Enter Sandman",
        *,
        tier2: bool = False,
        playlist_id: UUID | None = None,
    ) -> BroadcastTrackIdentity:
        name = f"Artist {len(self._artist_of)}"
        artist = BroadcastArtist(
            id=uuid4(),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=MatchStatus.PENDING if tier2 else MatchStatus.AUTO_MATCHED,
        )
        self.artists.upsert(artist)
        if not tier2:
            seed_link(self.links, self.catalog, artist.id, self._mbid(artist))
        norm = normalize_title(title)
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=artist.id,
                original_title=title,
                normalized_title=norm,
                normalized_signature=compute_normalized_signature(artist.normalized_name, norm),
            )
        )
        self.songs.register_playlist_identity(playlist_id or self.playlist_id, song.id)
        self._artist_of[song.id] = artist
        return song

    @staticmethod
    def _mbid(artist: BroadcastArtist) -> str:
        return f"mbid-{artist.id}"

    def mbid(self, song: BroadcastTrackIdentity) -> str:
        return self._mbid(self._artist_of[song.id])

    def file(
        self, song: BroadcastTrackIdentity, score: float, *, tagged: bool = False
    ) -> LibraryFile:
        """A file of the song's artist; ``tagged`` carries the artist MBID (a Step A
        candidate), otherwise only Step C or Tier 2 sees it. ``score`` pins its score."""
        artist = self._artist_of[song.id]
        library_file = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                track_title=song.original_title,
                normalized_title=song.normalized_title,
                artist_name=artist.original_name,
                normalized_artist_name=artist.normalized_name,
                artist_mbid=self._mbid(artist) if tagged else None,
            ),
        )
        self.files.upsert(library_file)
        self.scores[library_file.id] = score
        return library_file

    def old_suggestion(self, song: BroadcastTrackIdentity) -> Match:
        """A rewound song keeps its old suggestion: the re-check rewind is status only."""
        return self.matches.create(
            Match(
                id=uuid4(),
                identity_id=song.id,
                library_file_id=uuid4(),
                confidence_score=60.0,
                match_tier=MatchTier.LOCAL_FILE_FUZZY,
            )
        )

    def run(self, mb: FakeMbClient, playlist_id: UUID | None = None) -> list[str]:
        return match_identities_for_playlist(
            playlist_id=playlist_id or self.playlist_id,
            repos=IdentityMatchingRepos(
                track_identity_repo=self.songs,
                broadcast_artist_repo=self.artists,
                match_repo=self.matches,
                library_file_repo=self.files,
                rules_repo=FakeMappingRuleRepository(),
                catalog_repo=self.catalog,
                link_repo=self.links,
            ),
            mb_client=mb,
        )

    def stored(self, song: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return stored

    def decided(self, song: BroadcastTrackIdentity) -> tuple[MatchStatus, list[UUID | None]]:
        rows = [m.library_file_id for m in self.matches.rows_for(song.id)]
        return self.stored(song).match_status, rows

    def assert_deferred(self, song: BroadcastTrackIdentity) -> None:
        stored = self.stored(song)
        assert (
            stored.match_status,
            stored.match_tier,
            stored.reason_code,
            stored.reason_detail,
        ) == (
            MatchStatus.NEEDS_REVIEW,
            MatchTier.UNCLASSIFIED,
            ReasonCode.MB_UNAVAILABLE,
            reasons.format_mb_unavailable(),
        )
        assert self.matches.rows_for(song.id) == []  # no row, and the old suggestion is gone


# --- The new names -----------------------------------------------------------------------------


def test_the_cut_off_is_five_and_the_reason_code_is_its_own() -> None:
    assert mc.SONG_MB_FAILURE_CUTOFF == 5
    assert ReasonCode.MB_UNAVAILABLE.value == "MB_UNAVAILABLE"
    assert ReasonCode.MB_UNAVAILABLE != ReasonCode.DEFERRED_RETRY


# --- One song deferred, the playlist carries on -------------------------------------------------


@pytest.mark.parametrize("error", list(TRANSIENT.values()), ids=list(TRANSIENT))
def test_a_transient_step_b_failure_defers_the_song_and_the_playlist_carries_on(
    scores: Scores, error: BaseException
) -> None:
    rig = _Rig(scores)
    needs_b = rig.song()  # no file tagged with its artist's MBID: Step A is empty
    rig.old_suggestion(needs_b)
    by_step_a = rig.song("One")
    tagged = rig.file(by_step_a, 100.0, tagged=True)
    by_tier2 = rig.song("Peace Sells", tier2=True)
    named = rig.file(by_tier2, 70.0)
    mb = _Mb({rig.mbid(needs_b): error})

    rig.run(mb)  # returns: an outage is not a failed run

    rig.assert_deferred(needs_b)
    assert rig.decided(by_step_a) == (MatchStatus.AUTO_MATCHED, [tagged.id])
    assert rig.decided(by_tier2) == (MatchStatus.NEEDS_REVIEW, [named.id])
    assert mb.searches == 1


@pytest.mark.parametrize(
    ("score", "tagged"),
    [(70.0, True), (40.0, True), (100.0, False), (60.0, False)],
    ids=[
        "step_a_in_review",
        "step_a_under_the_floor",  # never auto_rejected: the floor is not applied
        "step_c_would_auto_match",  # Lance, 2026-10-12: deferred even then (Q2)
        "step_c_in_review",
    ],
)
def test_a_song_whose_step_b_failed_is_never_decided_on_partial_evidence(
    scores: Scores, score: float, tagged: bool
) -> None:
    rig = _Rig(scores)
    song = rig.song()
    rig.file(song, score, tagged=tagged)
    rig.old_suggestion(song)
    mb = _Mb({rig.mbid(song): _http_error(503)})

    rig.run(mb)

    rig.assert_deferred(song)
    assert mb.searches == 1


def test_songs_that_never_reach_step_b_are_decided_during_an_outage(scores: Scores) -> None:
    rig = _Rig(scores)
    by_step_a = rig.song("One")
    tagged = rig.file(by_step_a, 100.0, tagged=True)
    by_tier2 = rig.song("Peace Sells", tier2=True)
    named = rig.file(by_tier2, 100.0)
    mb = _Mb(down=_http_error(503))

    rig.run(mb)

    assert rig.decided(by_step_a) == (MatchStatus.AUTO_MATCHED, [tagged.id])
    assert rig.decided(by_tier2) == (MatchStatus.AUTO_MATCHED, [named.id])
    assert mb.searches == 0


# --- The cut-off -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answers", "outcomes", "searches"),
    [
        ("FFFFFFF", "DDDDDDD", 5),  # the 5th failure trips it: songs 6 and 7 make no call
        ("FFFFFS", "DDDDDD", 5),  # after the trip, a search that would succeed is not made
        ("FFFFS", "DDDDR", 5),  # four failures do not trip it
        ("FFFFSFFFFS", "DDDDRDDDDR", 10),  # a successful search resets the count
        ("FFFFCFFFFC", "DDDDRDDDDR", 10),  # so does an answer from the client's cache
    ],
)
def test_five_consecutive_failures_stop_musicbrainz_calls_for_the_rest_of_the_run(
    scores: Scores, answers: str, outcomes: str, searches: int
) -> None:
    """``answers``: what MusicBrainz would answer each song, in playlist order (F fails with a
    503, S succeeds with no recording, C does so from the client's cache). ``outcomes``: D
    deferred, R decided (no candidate at all: auto_rejected). Two songs that never reach
    Step B come last and are decided."""
    rig = _Rig(scores)
    songs = [rig.song() for _ in answers]
    by_step_a = rig.song("One")
    tagged = rig.file(by_step_a, 100.0, tagged=True)
    by_tier2 = rig.song("Peace Sells", tier2=True)
    named = rig.file(by_tier2, 100.0)
    mb = _Mb(
        {
            rig.mbid(song): _http_error(503)
            for song, answer in zip(songs, answers, strict=True)
            if answer == "F"
        },
        cached={rig.mbid(s) for s, answer in zip(songs, answers, strict=True) if answer == "C"},
    )

    rig.run(mb)

    assert mb.searches == searches
    for song, outcome in zip(songs, outcomes, strict=True):
        if outcome == "D":
            rig.assert_deferred(song)
        else:
            stored = rig.stored(song)
            assert (stored.match_status, stored.reason_code) == (
                MatchStatus.AUTO_REJECTED,
                ReasonCode.NO_LOCAL_FILES,
            )
    assert rig.decided(by_step_a) == (MatchStatus.AUTO_MATCHED, [tagged.id])
    assert rig.decided(by_tier2) == (MatchStatus.AUTO_MATCHED, [named.id])


def test_after_the_trip_a_step_a_escalation_is_deferred_without_a_call(scores: Scores) -> None:
    """Step B also runs as the escalation of a Step A result in review; after the trip that
    song is deferred too, and MusicBrainz is not called."""
    rig = _Rig(scores)
    for _ in range(5):
        rig.song()
    escalation = rig.song("One")
    rig.file(escalation, 70.0, tagged=True)
    mb = _Mb(down=_http_error(503))

    rig.run(mb)

    assert mb.searches == 5
    rig.assert_deferred(escalation)


def test_the_cut_off_is_per_run(scores: Scores) -> None:
    """Each playlist run starts a new count: the count is not module state."""
    rig = _Rig(scores)
    for _ in range(5):
        rig.song()
    second_playlist = uuid4()
    later = rig.song(playlist_id=second_playlist)
    mb = _Mb(down=_http_error(503))

    rig.run(mb)
    assert mb.searches == 5
    rig.run(mb, second_playlist)

    assert mb.searches == 6
    rig.assert_deferred(later)


@pytest.mark.parametrize(
    ("count", "failures", "trips", "cut_off"),
    [(7, [1, 2, 3, 4, 5], [5], True), (4, [1, 2, 3, 4], [], False)],
    ids=["tripped", "not_tripped"],
)
def test_the_failures_the_trip_and_the_deferrals_are_logged(
    scores: Scores, count: int, failures: list[int], trips: list[int], cut_off: bool
) -> None:
    """The worker log is the operator's only trace of an outage (no FAILED row): one warning
    per failed search, one when the cut-off trips, and the deferrals in the run summary."""
    rig = _Rig(scores)
    for _ in range(count):
        rig.song()

    with capture_logs() as logs:
        rig.run(_Mb(down=_http_error(503)))

    failed = [e for e in logs if e["event"] == "song_matching_mb_search_failed"]
    tripped = [e for e in logs if e["event"] == "song_matching_mb_cut_off"]
    (summary,) = [e for e in logs if e["event"] == "identity_matching_complete"]
    assert [e["log_level"] for e in failed] == ["warning"] * len(failures)
    assert [e["consecutive_failures"] for e in failed] == failures
    assert [(e["log_level"], e["consecutive_failures"]) for e in tripped] == [
        ("warning", n) for n in trips
    ]
    assert (summary["deferred"], summary["mb_cut_off"]) == (count, cut_off)


# --- What still fails, and what wins -----------------------------------------------------------


@pytest.mark.parametrize("error", list(PERMANENT.values()), ids=list(PERMANENT))
def test_a_permanent_musicbrainz_error_still_fails_the_run(
    scores: Scores, error: BaseException
) -> None:
    rig = _Rig(scores)
    song = rig.song()

    with pytest.raises(type(error)):
        rig.run(_Mb({rig.mbid(song): error}))

    assert rig.stored(song).match_status == MatchStatus.PENDING


def test_a_decision_made_meanwhile_wins_over_the_deferral(scores: Scores) -> None:
    """The guarded write (AUD-R018): the user decided while the run was matching. Their
    decision stays and the row it rests on is not deleted."""
    rig = _Rig(scores)
    song = rig.song()
    kept = rig.old_suggestion(song)
    rig.songs.decide_after_read[song.id] = MatchStatus.MANUAL_MATCHED
    mb = _Mb(down=_http_error(503))

    rig.run(mb)

    stored = rig.stored(song)
    assert (stored.match_status, stored.reason_code) == (MatchStatus.MANUAL_MATCHED, None)
    assert rig.matches.rows_for(song.id) == [kept]
    assert mb.searches == 1


# --- The re-check rewinds every outage song ----------------------------------------------------


class _Seeds:
    def __init__(self) -> None:
        self.artists = FakeBroadcastArtistRepository()
        self.songs = FakeBroadcastTrackIdentityRepository()

    def artist(
        self, name: str, status: MatchStatus, reason: ReasonCode | None = None
    ) -> BroadcastArtist:
        return self.artists.upsert(
            BroadcastArtist(
                id=uuid4(),
                original_name=name,
                normalized_name=name,
                match_status=status,
                reason_code=reason,
            )
        )

    def song(
        self,
        artist: BroadcastArtist,
        status: MatchStatus,
        reason: ReasonCode | None,
        playlist_id: UUID | None = None,
    ) -> BroadcastTrackIdentity:
        title = uuid4().hex
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=artist.id,
                original_title=title,
                normalized_title=title,
                normalized_signature=f"{artist.normalized_name}:{title}",
                match_status=status,
                match_tier=None if status == MatchStatus.PENDING else MatchTier.UNCLASSIFIED,
                reason_code=reason,
                reason_detail=None if reason is None else "stale reason",
            )
        )
        if playlist_id is not None:
            self.songs.register_playlist_identity(playlist_id, song.id)
        return song

    def stored(self, song: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return stored


def test_every_recheck_rewinds_the_outage_songs_whatever_their_artist() -> None:
    seeds = _Seeds()
    playlist = uuid4()
    outage_reason = ReasonCode.MB_UNAVAILABLE
    in_wave = seeds.artist("abba", MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE)
    matched = seeds.artist("queen", MatchStatus.AUTO_MATCHED)
    deferred_artist = seeds.artist("blondie", MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)
    wave_song = seeds.song(in_wave, MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE)
    outage = seeds.song(matched, MatchStatus.NEEDS_REVIEW, outage_reason, playlist)
    # Lance, 2026-10-12 (Q1): the artist cascade's deferrals keep artist matching's own path.
    cascaded = seeds.song(deferred_artist, MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)
    stale = seeds.song(matched, MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY)
    review = seeds.song(matched, MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE)
    # Approve writes status and tier only, so an approved outage song keeps the reason.
    approved = seeds.song(matched, MatchStatus.MANUAL_MATCHED, outage_reason)
    floored = seeds.song(matched, MatchStatus.AUTO_REJECTED, outage_reason)

    counts = rewind_wave({"abba"}, seeds.artists, seeds.songs)

    assert counts == RewindCounts(artists=1, songs=2)
    for song in (wave_song, outage):
        stored = seeds.stored(song)
        assert (stored.match_status, stored.match_tier, stored.reason_code) == (
            MatchStatus.PENDING,
            None,
            None,
        )
    for song, status, reason in (
        (cascaded, MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY),
        (stale, MatchStatus.NEEDS_REVIEW, ReasonCode.DEFERRED_RETRY),
        (review, MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE),
        (approved, MatchStatus.MANUAL_MATCHED, outage_reason),
        (floored, MatchStatus.AUTO_REJECTED, outage_reason),
    ):
        stored = seeds.stored(song)
        assert (stored.match_status, stored.reason_code) == (status, reason)
    # Artists follow the wave only: the deferred artist waits for its own playlist's reset.
    stored_artist = seeds.artists.get_by_id(deferred_artist.id)
    assert stored_artist is not None
    assert stored_artist.match_status == MatchStatus.NEEDS_REVIEW
    assert playlists_with_pending_work(seeds.artists, seeds.songs) == {playlist}


def test_scope_all_rewinds_an_outage_song_once() -> None:
    seeds = _Seeds()
    matched = seeds.artist("queen", MatchStatus.AUTO_MATCHED)
    seeds.song(matched, MatchStatus.NEEDS_REVIEW, ReasonCode.MB_UNAVAILABLE)

    counts = rewind_wave(None, seeds.artists, seeds.songs)

    assert counts == RewindCounts(artists=0, songs=1)


def test_an_empty_wave_still_rewinds_the_outage_songs() -> None:
    """A changed re-check with nothing indexed or linked since its watermark has an empty
    wave (no artist is rewound), yet every outage song is: MusicBrainz coming back changes
    no file, so no wave would ever name it."""
    seeds = _Seeds()
    playlist = uuid4()
    matched = seeds.artist("queen", MatchStatus.AUTO_MATCHED)
    seeds.song(matched, MatchStatus.NEEDS_REVIEW, ReasonCode.MB_UNAVAILABLE, playlist)

    counts = rewind_wave(set(), seeds.artists, seeds.songs)

    assert counts == RewindCounts(artists=0, songs=1)
    assert playlists_with_pending_work(seeds.artists, seeds.songs) == {playlist}
