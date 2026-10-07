"""Acceptance tests: the 56% song floor and the song matching rules that ship with it.

Spec 2026-10-05 §4.3 and §2: D6 (floor), D12 (no song mid-band), D13 (tagged and untagged files).
- D6: a best candidate under SONG_MIN_PRESENTATION_SCORE (56), or no candidate at all, is
  auto_rejected and writes no match row. ORPHANED_IDENTITY and MISSING_MATCH_RECORD stay
  needs_review (data faults the curator should see).
- D12: a song never auto-matches below 65; 56-64 is needs_review whatever its lead. The rules at
  65 and above are unchanged.
- D13: when the artist-MBID steps (A, B) do not auto-match, Step C also scores every file of the
  artist, tagged or not; the better score wins and a tie keeps the MBID result.
- Artist matching keeps MID_BAND_LOWER 55, its mid-band and MIN_PRESENTATION_SCORE 50.

Candidate scores are pinned per file by patching _candidate_scores, so every boundary is exact.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from backend.config import Settings
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.services import matching_constants as mc
from backend.services.artist_matching_service import _decide_artist_zone
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    _score_candidates,
    match_identities_for_playlist,
)
from backend.services.matching_reasons import format_low_confidence
from backend.services.missing_file_reconciliation_service import _release_matches
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from backend.services.title_scoring import broadcast_title_variants
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient

ARTIST = "Metallica"
NORM = normalize_artist(ARTIST)
MBID = "mbid-metallica"
TITLE = "Enter Sandman"

Scores = dict[UUID, float]


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


def _file(
    *, title: str = TITLE, artist_mbid: str | None = None, recording_mbid: str | None = None
) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{uuid4().hex}.flac",
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        audio=AudioMetadata(
            track_title=title,
            normalized_title=normalize_title(title),
            artist_name=ARTIST,
            normalized_artist_name=NORM,
            artist_mbid=artist_mbid,
            recording_mbid=recording_mbid,
        ),
    )


def _scored(scores: Scores, *values: float) -> list[LibraryFile]:
    files = [_file() for _ in values]
    for f, value in zip(files, values, strict=True):
        scores[f.id] = value
    return files


# --- Constants: song-only, artist untouched ---------------------------------------------------


def test_the_song_floor_is_56_and_sits_below_quick_review() -> None:
    assert mc.SONG_MIN_PRESENTATION_SCORE == 56
    assert mc.SONG_MIN_PRESENTATION_SCORE < mc.QUICK_REVIEW_MIN_SCORE


def test_artist_thresholds_are_unchanged() -> None:
    assert mc.MID_BAND_LOWER == 55
    assert mc.MID_BAND_UPPER == 64
    assert mc.MID_BAND_GAP_THRESHOLD == 5
    assert mc.MIN_PRESENTATION_SCORE == 50
    assert Settings.model_fields["min_presentation_score"].default == 50


def test_artist_zones_keep_the_55_mid_band() -> None:
    """D6 and D12 are songs only: an artist at 55.5 or 60 with a 6-point lead auto-matches."""
    assert _decide_artist_zone(55.5, 6.0, 80, 10, True) == (MatchStatus.AUTO_MATCHED, None, None)
    assert _decide_artist_zone(60.0, 6.0, 80, 10, True) == (MatchStatus.AUTO_MATCHED, None, None)
    status, reason, _ = _decide_artist_zone(52.0, 100.0, 80, 10, False)
    assert (status, reason) == (MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE)


# --- _score_candidates ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("score", "status"),
    [
        (0.0, MatchStatus.AUTO_REJECTED),
        (55.0, MatchStatus.AUTO_REJECTED),
        (55.99, MatchStatus.AUTO_REJECTED),
        (56.0, MatchStatus.NEEDS_REVIEW),
        (60.0, MatchStatus.NEEDS_REVIEW),
        (64.99, MatchStatus.NEEDS_REVIEW),
        (65.0, MatchStatus.NEEDS_REVIEW),
    ],
)
def test_a_lone_candidate_under_56_is_auto_rejected(
    scores: Scores, score: float, status: MatchStatus
) -> None:
    (only,) = _scored(scores, score)

    result = _score_candidates(TITLE, [only], MatchTier.LOCAL_FILE_FUZZY, 80)

    assert result.status == status
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE
    # The raw score is floored; the text rounds (55.99 reads "Score 56%"). Cosmetic, accepted.
    assert result.reason_detail == format_low_confidence(score)
    assert result.confidence_score == score
    # The result still names its best file; the service writes no row for it (Plan note 6).
    assert result.library_file_id == only.id


@pytest.mark.parametrize(
    ("top", "runner_up", "status"),
    [
        (56.0, 50.0, MatchStatus.NEEDS_REVIEW),  # gap 6: the old song mid-band auto-matched it
        (60.0, 54.0, MatchStatus.NEEDS_REVIEW),  # gap 6
        (64.0, 59.0, MatchStatus.NEEDS_REVIEW),  # gap 5, the old upper edge
        (64.99, 0.0, MatchStatus.NEEDS_REVIEW),  # a lead of 65 points is still not enough
        (60.0, 57.0, MatchStatus.NEEDS_REVIEW),  # gap 3
        (58.0, 54.0, MatchStatus.NEEDS_REVIEW),  # gap 4; a runner-up under the floor is irrelevant
        (55.5, 49.0, MatchStatus.AUTO_REJECTED),  # gap 6.5 under the floor: no mid-band rescue
    ],
)
def test_a_song_never_auto_matches_below_65(
    scores: Scores, top: float, runner_up: float, status: MatchStatus
) -> None:
    """D12: no song mid-band. 56-64 is needs_review whatever the lead over the runner-up."""
    files = _scored(scores, top, runner_up)

    result = _score_candidates(TITLE, files, MatchTier.LOCAL_FILE_FUZZY, 80)

    assert result.status == status
    assert result.reason_code == ReasonCode.LOW_CONFIDENCE
    assert result.library_file_id == files[0].id


@pytest.mark.parametrize(
    ("values", "status", "reason"),
    [
        ((96.0,), MatchStatus.AUTO_MATCHED, None),  # MB_AUTO_LINK_SCORE, lone
        ((85.0, 70.0), MatchStatus.AUTO_MATCHED, None),  # strong (80) with a 10+ lead
        ((85.0, 80.0), MatchStatus.NEEDS_REVIEW, ReasonCode.AMBIGUOUS_GAP),  # strong, lead 5
        ((79.0, 50.0), MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE),  # under strong
        ((70.0,), MatchStatus.NEEDS_REVIEW, ReasonCode.LOW_CONFIDENCE),  # lone, under 95
    ],
)
def test_the_song_rules_from_65_up_are_unchanged(
    scores: Scores, values: tuple[float, ...], status: MatchStatus, reason: ReasonCode | None
) -> None:
    files = _scored(scores, *values)

    result = _score_candidates(TITLE, files, MatchTier.LOCAL_FILE_FUZZY, 80)

    assert (result.status, result.reason_code) == (status, reason)


# --- match_identities_for_playlist ------------------------------------------------------------


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


class _Files(FakeLibraryFileRepository):
    """Counts the Step C (name) lookups."""

    def __init__(self) -> None:
        super().__init__()
        self.name_lookups = 0

    def get_by_normalized_artist_name(
        self, normalized_name: str, limit: int = 100
    ) -> list[LibraryFile]:
        self.name_lookups += 1
        return super().get_by_normalized_artist_name(normalized_name, limit)


class _Rig:
    """One playlist, one Metallica artist and one song.

    resolved=True gives an AUTO_MATCHED artist whose artist match row targets ``artist_target``
    (None: no row at all), so the song goes through Tier 1; otherwise Tier 2.
    """

    def __init__(
        self, scores: Scores, *, resolved: bool = False, artist_target: str | None = MBID
    ) -> None:
        self.scores = scores
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = _Songs()
        self.matches = _Matches()
        self.files = _Files()
        self.artist = BroadcastArtist(
            id=uuid4(),
            original_name=ARTIST,
            normalized_name=NORM,
            match_status=MatchStatus.AUTO_MATCHED if resolved else MatchStatus.PENDING,
        )
        self.artists.upsert(self.artist)
        if resolved and artist_target is not None:
            self.matches.create(
                Match(
                    id=uuid4(),
                    artist_id=self.artist.id,
                    target_id=artist_target,
                    target_type=TargetType.ARTIST,
                    confidence_score=100.0,
                    match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
                )
            )

    def file(
        self,
        score: float | None = None,
        *,
        title: str = TITLE,
        artist_mbid: str | None = None,
        recording_mbid: str | None = None,
    ) -> LibraryFile:
        """A same-artist file; ``score`` pins its score (None: real title scoring)."""
        library_file = _file(title=title, artist_mbid=artist_mbid, recording_mbid=recording_mbid)
        self.files.upsert(library_file)
        if score is not None:
            self.scores[library_file.id] = score
        return library_file

    def song(
        self, rejected: tuple[UUID, ...] = (), *, title: str = TITLE
    ) -> BroadcastTrackIdentity:
        norm = normalize_title(title)
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=self.artist.id,
                original_title=title,
                normalized_title=norm,
                normalized_signature=compute_normalized_signature(NORM, norm),
                rejected_file_ids=rejected,
            )
        )
        self.songs.register_playlist_identity(self.playlist_id, song.id)
        return song

    def old_suggestion(self, song: BroadcastTrackIdentity) -> Match:
        """A rewound song keeps its old suggestion: the re-check rewind is status only (§4.2)."""
        return self.matches.create(
            Match(
                id=uuid4(),
                identity_id=song.id,
                library_file_id=uuid4(),
                confidence_score=60.0,
                match_tier=MatchTier.LOCAL_FILE_FUZZY,
            )
        )

    def run(self, mb: FakeMbClient | None = None) -> None:
        match_identities_for_playlist(
            playlist_id=self.playlist_id,
            repos=IdentityMatchingRepos(
                track_identity_repo=self.songs,
                broadcast_artist_repo=self.artists,
                match_repo=self.matches,
                library_file_repo=self.files,
                rules_repo=FakeMappingRuleRepository(),
                catalog_repo=FakeArtistRepository(),
            ),
            mb_client=mb or FakeMbClient(),
        )

    def stored(self, song: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return stored


@pytest.mark.parametrize("resolved", [False, True], ids=["tier2", "tier1_step_c"])
@pytest.mark.parametrize(
    ("score", "status", "rows"),
    [(55.99, MatchStatus.AUTO_REJECTED, 0), (56.0, MatchStatus.NEEDS_REVIEW, 1)],
    ids=["under_56", "at_56"],
)
def test_the_floor_decides_the_status_and_the_match_row(
    scores: Scores, resolved: bool, score: float, status: MatchStatus, rows: int
) -> None:
    rig = _Rig(scores, resolved=resolved)
    best = rig.file(score)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == status
    assert stored.match_tier == MatchTier.LOCAL_FILE_FUZZY
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert stored.reason_detail == format_low_confidence(score)
    found = [(m.library_file_id, m.confidence_score) for m in rig.matches.rows_for(song.id)]
    assert found == [(best.id, score)][:rows]


@pytest.mark.parametrize(
    ("resolved", "tier", "reason"),
    [
        (False, MatchTier.LOCAL_FILE_FUZZY, ReasonCode.NO_CANDIDATES),
        (True, MatchTier.MUSICBRAINZ_ID_SEARCH, ReasonCode.NO_LOCAL_FILES),
    ],
    ids=["tier2", "tier1"],
)
def test_no_candidate_is_auto_rejected_without_a_match_row(
    scores: Scores, resolved: bool, tier: MatchTier, reason: ReasonCode
) -> None:
    rig = _Rig(scores, resolved=resolved)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert (stored.match_status, stored.match_tier, stored.reason_code) == (
        MatchStatus.AUTO_REJECTED,
        tier,
        reason,
    )
    assert rig.matches.rows_for(song.id) == []


@pytest.mark.parametrize("artist_target", [None, "   "], ids=["no_artist_row", "blank_target"])
def test_a_missing_match_record_stays_in_review(scores: Scores, artist_target: str | None) -> None:
    rig = _Rig(scores, resolved=True, artist_target=artist_target)
    rig.file(100.0)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.MISSING_MATCH_RECORD
    assert rig.matches.rows_for(song.id) == []


def test_step_b_still_rescues_a_step_a_result_under_the_floor(scores: Scores) -> None:
    """A floored Step A result is not AUTO_MATCHED, so the MB recording search still runs."""
    rig = _Rig(scores, resolved=True)
    rig.file(40.0, artist_mbid=MBID)
    step_b = rig.file(70.0, recording_mbid="rec-1")
    song = rig.song()
    mb = FakeMbClient(
        recording_searches={(MBID, broadcast_title_variants(TITLE)[0]): [{"id": "rec-1"}]}
    )

    rig.run(mb)

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_SEARCH
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [step_b.id]


def test_a_step_a_and_step_b_tie_in_review_keeps_step_a(scores: Scores) -> None:
    """The Step A / Step B tie rule above the floor: equal scores keep Step A's tier and file."""
    rig = _Rig(scores, resolved=True)
    step_a = rig.file(60.0, artist_mbid=MBID)
    rig.file(60.0, recording_mbid="rec-1")
    song = rig.song()
    mb = FakeMbClient(
        recording_searches={(MBID, broadcast_title_variants(TITLE)[0]): [{"id": "rec-1"}]}
    )

    rig.run(mb)

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [step_a.id]


# --- D13: Step C also scores the artist's untagged files ------------------------------------


def test_an_untagged_exact_file_beats_a_tagged_wrong_song() -> None:
    """The Bad Company shape (dev DB): the artist's MBID-tagged files hold only a wrong song,
    while the right one is an untagged file with the exact title. Real title scoring."""
    rig = _Rig({}, resolved=True)
    rig.file(title="Shooting Star", artist_mbid=MBID)
    untagged = rig.file(title="If You Needed Somebody")
    song = rig.song(title="If You Needed Somebody")
    mb = FakeMbClient()

    rig.run(mb)

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.LOCAL_FILE_FUZZY
    rows = rig.matches.rows_for(song.id)
    assert [(m.library_file_id, m.confidence_score) for m in rows] == [(untagged.id, 100.0)]
    assert len(mb.calls) == 1  # Step B ran once, as today; Step C is local


def test_a_better_untagged_file_wins_in_review(scores: Scores) -> None:
    rig = _Rig(scores, resolved=True)
    rig.file(40.0, artist_mbid=MBID)
    untagged = rig.file(60.0)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.LOCAL_FILE_FUZZY
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [untagged.id]


def test_step_c_also_beats_a_step_b_result(scores: Scores) -> None:
    rig = _Rig(scores, resolved=True)
    rig.file(60.0, recording_mbid="rec-1")
    untagged = rig.file(75.0)
    song = rig.song()
    mb = FakeMbClient(
        recording_searches={(MBID, broadcast_title_variants(TITLE)[0]): [{"id": "rec-1"}]}
    )

    rig.run(mb)

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.LOCAL_FILE_FUZZY
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [untagged.id]


def test_a_step_c_win_auto_matches_on_a_strong_lead(scores: Scores) -> None:
    """The winner's status is decided over Step C's own set: 90 with a 30-point lead."""
    rig = _Rig(scores, resolved=True)
    rig.file(60.0, artist_mbid=MBID)
    untagged = rig.file(90.0)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert stored.match_tier == MatchTier.LOCAL_FILE_FUZZY
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [untagged.id]


def test_a_step_c_win_under_56_is_still_floored(scores: Scores) -> None:
    """The raw scores are compared first; the winner keeps its own (floored) status."""
    rig = _Rig(scores, resolved=True)
    rig.file(40.0, artist_mbid=MBID)
    rig.file(50.0)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.AUTO_REJECTED
    assert stored.match_tier == MatchTier.LOCAL_FILE_FUZZY
    assert stored.reason_detail == format_low_confidence(50.0)
    assert rig.matches.rows_for(song.id) == []


def test_a_tie_keeps_the_mbid_result(scores: Scores) -> None:
    rig = _Rig(scores, resolved=True)
    tagged = rig.file(70.0, artist_mbid=MBID)
    rig.file(70.0)
    song = rig.song()

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [tagged.id]


def test_a_rejected_untagged_file_is_still_skipped(scores: Scores) -> None:
    rig = _Rig(scores, resolved=True)
    tagged = rig.file(60.0, artist_mbid=MBID)
    rejected = rig.file(100.0)
    song = rig.song(rejected=(rejected.id,))

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.match_tier == MatchTier.MUSICBRAINZ_ID_EXACT
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [tagged.id]


def test_step_c_does_not_run_when_step_a_auto_matched(scores: Scores) -> None:
    rig = _Rig(scores, resolved=True)
    tagged = rig.file(100.0, artist_mbid=MBID)
    rig.file(60.0)
    song = rig.song()
    mb = FakeMbClient()

    rig.run(mb)

    assert rig.stored(song).match_status == MatchStatus.AUTO_MATCHED
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [tagged.id]
    assert rig.files.name_lookups == 0
    assert mb.calls == []


@pytest.mark.parametrize("resolved", [False, True], ids=["tier2", "tier1_step_c"])
def test_the_name_lookup_scores_every_file_of_a_large_artist(
    scores: Scores, resolved: bool
) -> None:
    """The name lookup is a bound, not a sample: the 101st file by id is still scored.

    Every file is untagged, so Tier 1 reaches Step C on master too and the test isolates the cap.
    """
    rig = _Rig(scores, resolved=resolved)
    files = [rig.file(10.0) for _ in range(101)]
    last = max(files, key=lambda f: str(f.id))
    scores[last.id] = 100.0
    song = rig.song()

    rig.run()

    assert rig.stored(song).match_status == MatchStatus.AUTO_MATCHED
    assert [m.library_file_id for m in rig.matches.rows_for(song.id)] == [last.id]


# --- Stale rows, rejections, decisions, file deletion ----------------------------------------


def test_an_old_suggestion_goes_when_the_song_now_scores_under_56(scores: Scores) -> None:
    rig = _Rig(scores)
    rig.file(40.0)
    song = rig.song()
    rig.old_suggestion(song)

    rig.run()

    assert rig.stored(song).match_status == MatchStatus.AUTO_REJECTED
    assert rig.matches.rows_for(song.id) == []


def test_a_rejected_best_file_leaves_a_runner_up_under_56_auto_rejected(scores: Scores) -> None:
    rig = _Rig(scores)
    best = rig.file(100.0)
    rig.file(40.0)
    song = rig.song(rejected=(best.id,))

    rig.run()

    stored = rig.stored(song)
    assert stored.match_status == MatchStatus.AUTO_REJECTED
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert rig.matches.rows_for(song.id) == []


def test_a_decision_made_meanwhile_beats_a_floored_result(scores: Scores) -> None:
    rig = _Rig(scores)
    rig.file(40.0)
    song = rig.song()
    old = rig.old_suggestion(song)
    rig.songs.decide_after_read[song.id] = MatchStatus.MANUAL_MATCHED

    rig.run()

    assert rig.stored(song).match_status == MatchStatus.MANUAL_MATCHED
    assert rig.matches.rows_for(song.id) == [old]


def test_deleting_the_best_file_of_a_floored_song_does_not_reopen_it(scores: Scores) -> None:
    """Why AUTO_REJECTED writes no row: deleting a missing file sends a song whose rows pointed
    at it, and that is left with none, back to review (_release_matches, LIBRARY_FILE_REMOVED)."""
    rig = _Rig(scores)
    best = rig.file(40.0)
    song = rig.song()
    rig.run()

    released = _release_matches(best.id, rig.matches, rig.songs)

    assert released == 0
    assert rig.stored(song).match_status == MatchStatus.AUTO_REJECTED
