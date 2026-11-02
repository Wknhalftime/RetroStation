"""Acceptance: song matching in trial mode (SPEC 2026-11-01, S3-S9, E2-E5; AUD-R100, AUD-R101).

Lance: "don't remove the human from the equation." Until V11.1 passes his blind test, today's
title scorer and V11.1 both decide each song:
- auto-match only when both auto-match the same file (the result is today's);
- hidden whenever V11.1 hides it (AUD-R101; when both hide it the result is today's, and
  test_title_v11_hides.py covers V11.1 hiding a song today's scorer shows);
- review otherwise, showing V11.1's pick.

T is a song's result from today's strategy chain, V the same chain scored by V11.1. V11.1's
policy: auto-match at 95, or at 85 with a 10-point lead over a real competitor; hidden under 56.
match_identities_for_playlist takes V11.1 as ``trial_title_score`` (log title, file title ->
score); without it, matching is today's (S10).

Today's scores are pinned per file by patching _candidate_scores, as test_step_b_reach.py does;
V11.1's are pinned per file title by the fake ``trial_title_score``. A few tests use both real
scorers on real titles.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import EnrichmentStatus, MatchStatus, MatchTier, ReasonCode, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.services.identity_matching_service import (
    IdentityMatchingRepos,
    match_identities_for_playlist,
)
from backend.services.matching_reasons import format_low_confidence
from backend.services.normalization import (
    compute_normalized_signature,
    normalize_artist,
    normalize_title,
)
from backend.services.title_scoring import broadcast_title_variants, v11_title_score
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.broadcast_artists import FakeBroadcastArtistRepository
from tests.fakes.broadcast_track_identities import FakeBroadcastTrackIdentityRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.mapping_rules import FakeMappingRuleRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.recording_search_reach import FakeRecordingSearchReach

ARTIST = "Blur"
SONG = "Song 2"
Row = tuple[UUID | None, float, MatchTier | None]


class _Mb(FakeMbClient):
    """FakeMbClient that counts recording searches and can be down (an outage)."""

    def __init__(
        self,
        recording_searches: dict[tuple[str, str], list[dict[str, Any]]],
        down: BaseException | None,
    ) -> None:
        super().__init__(recording_searches=recording_searches)
        self.down = down

    def search_recording(
        self, artist_mbid: str, title: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        found = super().search_recording(artist_mbid, title, limit)
        if self.down is not None:
            raise self.down
        return found

    @property
    def searches(self) -> int:
        return sum(1 for call in self.calls if call.startswith("search_recording:"))


class _World:
    """One playlist, one artist (auto-matched to ``mbid-Blur`` unless ``resolved=False``)."""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, *, pin_today: bool = True, resolved: bool = True
    ) -> None:
        self.playlist_id = uuid4()
        self.artists = FakeBroadcastArtistRepository()
        self.songs = FakeBroadcastTrackIdentityRepository()
        self.matches = FakeMatchRepository()
        self.files = FakeLibraryFileRepository()
        self.rules = FakeMappingRuleRepository()
        self.recording_searches: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.today: dict[UUID, float] = {}
        self.v11: dict[str, float] = {}
        self.v11_calls: list[tuple[str, str]] = []
        self.mbid = f"mbid-{ARTIST}"
        if pin_today:
            monkeypatch.setattr(
                "backend.services.identity_matching_service._candidate_scores",
                lambda _full, _core, f, _threshold: (self.today[f.id], self.today[f.id]),
            )
        self.artist = BroadcastArtist(
            id=uuid4(),
            original_name=ARTIST,
            normalized_name=normalize_artist(ARTIST),
            match_status=MatchStatus.AUTO_MATCHED if resolved else MatchStatus.NEEDS_REVIEW,
        )
        self.artists.upsert(self.artist)

    def link_artist(self) -> None:
        """The artist's match row, which a resolved artist's songs need (Tier 1)."""
        self.matches.create(
            Match(
                id=uuid4(),
                artist_id=self.artist.id,
                target_id=self.mbid,
                target_type=TargetType.ARTIST,
                confidence_score=100.0,
                match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
            )
        )

    def song(self, title: str = SONG) -> BroadcastTrackIdentity:
        norm = normalize_title(title)
        song = self.songs.upsert(
            BroadcastTrackIdentity(
                id=uuid4(),
                broadcast_artist_id=self.artist.id,
                original_title=title,
                normalized_title=norm,
                normalized_signature=compute_normalized_signature(
                    self.artist.normalized_name, norm
                ),
            )
        )
        self.songs.register_playlist_identity(self.playlist_id, song.id)
        return song

    def file(
        self,
        title: str | None,
        today: float | None = None,
        v11: float | None = None,
        *,
        tagged: bool = False,
        recording_mbid: str | None = None,
        normalized_title: str | None = None,
    ) -> LibraryFile:
        """A file of the artist. ``today`` pins today's score, ``v11`` V11.1's (by title)."""
        library_file = LibraryFile(
            id=uuid4(),
            file_path=f"/music/{uuid4().hex}.flac",
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
            audio=AudioMetadata(
                track_title=title,
                normalized_title=normalized_title or (normalize_title(title) if title else None),
                recording_mbid=recording_mbid,
                artist_mbid=self.mbid if tagged else None,
                artist_name=ARTIST,
                normalized_artist_name=normalize_artist(ARTIST),
            ),
        )
        self.files.upsert(library_file)
        if today is not None:
            self.today[library_file.id] = today
        if v11 is not None:
            assert title is not None
            self.v11[title] = v11
        return library_file

    def found_by_search(self, song_title: str, recording_mbid: str) -> None:
        """MusicBrainz returns this recording when the artist is searched with the title."""
        key = (self.mbid, broadcast_title_variants(song_title)[0])
        self.recording_searches.setdefault(key, []).append({"id": recording_mbid})

    def fake_v11(self, log_title: str, file_title: str) -> float:
        self.v11_calls.append((log_title, file_title))
        return self.v11[file_title]

    def run(
        self, *, trial: bool = True, real_v11: bool = False, down: Exception | None = None
    ) -> _Mb:
        mb = _Mb(self.recording_searches, down)
        repos = IdentityMatchingRepos(
            track_identity_repo=self.songs,
            broadcast_artist_repo=self.artists,
            match_repo=self.matches,
            library_file_repo=self.files,
            rules_repo=self.rules,
            catalog_repo=FakeArtistRepository(),
            recording_search_reach=FakeRecordingSearchReach(self.files),
        )
        score = v11_title_score if real_v11 else self.fake_v11
        if trial:
            match_identities_for_playlist(self.playlist_id, repos, mb, trial_title_score=score)
        else:
            match_identities_for_playlist(self.playlist_id, repos, mb)
        return mb

    def stored(self, song: BroadcastTrackIdentity) -> BroadcastTrackIdentity:
        stored = self.songs.get_by_id(song.id)
        assert stored is not None
        return stored

    def rows(self, song: BroadcastTrackIdentity) -> list[Row]:
        return [
            (m.library_file_id, m.confidence_score, m.match_tier)
            for m in self.matches._data.values()
            if m.identity_id == song.id
        ]


def _resolved(monkeypatch: pytest.MonkeyPatch, **kwargs: bool) -> _World:
    world = _World(monkeypatch, **kwargs)
    world.link_artist()
    return world


# --- S3: auto-match needs both, on the same file --------------------------------------------------


def test_auto_needs_both_same_file(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    a = world.file("A", today=100.0, v11=97.0)
    world.file("B", today=50.0, v11=40.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.AUTO_MATCHED
    assert (stored.reason_code, stored.reason_detail) == (None, None)
    # The result is today's: today's score on the match row.
    assert world.rows(song) == [(a.id, 100.0, MatchTier.LOCAL_FILE_FUZZY)]


def test_both_auto_on_different_files_goes_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    world.file("A", today=100.0, v11=40.0)
    b = world.file("B", today=50.0, v11=97.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    assert world.rows(song) == [(b.id, 97.0, MatchTier.LOCAL_FILE_FUZZY)]


# --- S4: hidden needs both ------------------------------------------------------------------------


def test_hidden_needs_both(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    world.file("A", today=40.0, v11=30.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.AUTO_REJECTED
    # Today's result: today's floor reason and score text, and no match row (AUD-R022 D6).
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert stored.reason_detail == format_low_confidence(40.0)
    assert world.rows(song) == []


def test_hidden_today_shown_by_v11_goes_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    a = world.file("A", today=40.0, v11=60.0)
    song = world.song()

    world.run()

    assert world.stored(song).match_status == MatchStatus.NEEDS_REVIEW
    assert world.rows(song) == [(a.id, 60.0, MatchTier.LOCAL_FILE_FUZZY)]


# --- S5: review shows V11.1's pick unless V11.1 hides the song ------------------------------------


@pytest.mark.parametrize(
    ("scores", "shown", "shown_score"),
    [
        # both review, different picks: V11.1's
        ({"A": (70.0, 50.0), "B": (65.0, 70.0)}, "B", 70.0),
        # today auto-matches, V11.1 reviews the same file: V11.1's score
        ({"A": (100.0, 70.0), "B": (50.0, 30.0)}, "A", 70.0),
    ],
    ids=["both_review", "today_auto_v11_review"],
)
def test_review_shows_v11_pick_unless_hidden(
    monkeypatch: pytest.MonkeyPatch,
    scores: dict[str, tuple[float, float]],
    shown: str,
    shown_score: float,
) -> None:
    world = _resolved(monkeypatch)
    files = {title: world.file(title, today=t, v11=v) for title, (t, v) in scores.items()}
    song = world.song()

    world.run()

    assert world.stored(song).match_status == MatchStatus.NEEDS_REVIEW
    assert world.rows(song) == [(files[shown].id, shown_score, MatchTier.LOCAL_FILE_FUZZY)]


# --- S6: the reason -------------------------------------------------------------------------------


def test_disagreement_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    world.file("A", today=97.0, v11=71.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    assert stored.reason_detail is not None
    assert "97" in stored.reason_detail
    assert "71" in stored.reason_detail


def test_agreement_keeps_the_shown_sides_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    world.file("A", today=70.0, v11=66.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.LOW_CONFIDENCE
    assert stored.reason_detail == format_low_confidence(66.0)


# --- S7: each chain walks its own tiers -----------------------------------------------------------


def test_each_scorer_walks_its_own_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's chain stops at Step A's auto-match; V11.1's does not, and goes on to Step C."""
    world = _resolved(monkeypatch)
    world.file("Tagged", today=100.0, v11=60.0, tagged=True)
    untagged = world.file("Untagged", today=50.0, v11=100.0)
    song = world.song()

    world.run()

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    assert world.rows(song) == [(untagged.id, 100.0, MatchTier.LOCAL_FILE_FUZZY)]


# --- S8: results with no title score are unchanged ------------------------------------------------


def _no_files(world: _World) -> None:
    world.link_artist()


def _missing_match_record(world: _World) -> None:
    world.file("A", today=100.0, v11=40.0)


def _mapping_rule(world: _World) -> None:
    world.link_artist()
    target = world.file("A", today=40.0, v11=30.0)
    signature = compute_normalized_signature(world.artist.normalized_name, normalize_title(SONG))
    world.rules.create(
        MappingRule(
            id=uuid4(),
            source_pattern=signature,
            target_type=TargetType.LIBRARY_FILE,
            target_id=str(target.id),
        )
    )


@pytest.mark.parametrize(
    ("setup", "resolved"),
    [(_no_files, True), (_missing_match_record, True), (_mapping_rule, True), (None, False)],
    ids=["no_local_files", "missing_match_record", "mapping_rule", "no_candidates"],
)
def test_unscored_results_unchanged(
    monkeypatch: pytest.MonkeyPatch, setup: Any, resolved: bool
) -> None:
    outcomes = []
    for trial in (True, False):
        world = _World(monkeypatch, resolved=resolved)
        if setup is not None:
            setup(world)
        song = world.song()
        world.run(trial=trial)
        stored = world.stored(song)
        rows = sorted((score, tier) for _f, score, tier in world.rows(song))
        outcomes.append(
            (stored.match_status, stored.match_tier, stored.reason_code, stored.reason_detail, rows)
        )

    with_trial, today = outcomes
    assert with_trial == today


# --- S9 and E3: one MusicBrainz search per song, shared -------------------------------------------


def _step_a_auto_but_not_for_v11(world: _World) -> BroadcastTrackIdentity:
    """Today auto-matches at Step A and stops; V11.1 needs Step B, which can add a file."""
    world.file("Tagged", today=100.0, v11=60.0, tagged=True)
    world.file("Searched", today=50.0, v11=70.0, recording_mbid="rec-1")
    world.found_by_search(SONG, "rec-1")
    return world.song()


def test_one_search_per_song(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    song = _step_a_auto_but_not_for_v11(world)

    mb = world.run()

    assert mb.searches == 1
    assert world.stored(song).match_status == MatchStatus.NEEDS_REVIEW


def test_one_search_when_both_chains_need_step_b(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch)
    world.file("Searched", today=70.0, v11=60.0, recording_mbid="rec-1")
    world.found_by_search(SONG, "rec-1")
    world.song()

    mb = world.run()

    assert mb.searches == 1


def test_mb_outage_defers_song(monkeypatch: pytest.MonkeyPatch) -> None:
    """E3: V11.1's chain cannot finish without MusicBrainz, so the song waits (AUD-R028)."""
    world = _resolved(monkeypatch)
    song = _step_a_auto_but_not_for_v11(world)

    world.run(down=httpx.ConnectError("connection refused"))

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.MB_UNAVAILABLE
    assert world.rows(song) == []


# --- E2, E4, E5 -----------------------------------------------------------------------------------


def test_missing_track_title(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2: a file with no track_title is scored by V11.1 against its normalized_title."""
    world = _resolved(monkeypatch)
    world.file(None, today=70.0, normalized_title="song 2")
    world.v11["song 2"] = 70.0
    world.song()

    world.run()

    assert set(world.v11_calls) == {(SONG, "song 2")}


@pytest.mark.parametrize(
    ("v11", "status"),
    [(90.0, MatchStatus.NEEDS_REVIEW), (95.0, MatchStatus.AUTO_MATCHED)],
    ids=["90_needs_a_competitor", "95_alone_is_enough"],
)
def test_lone_candidate_needs_95(
    monkeypatch: pytest.MonkeyPatch, v11: float, status: MatchStatus
) -> None:
    """E4: with no real competitor, V11.1 auto-matches only at 95."""
    world = _resolved(monkeypatch)
    world.file("A", today=100.0, v11=v11)
    song = world.song()

    world.run()

    assert world.stored(song).match_status == status


@pytest.mark.parametrize("plain_first", [True, False], ids=["plain_first", "live_first"])
def test_tie_break_is_stable(monkeypatch: pytest.MonkeyPatch, plain_first: bool) -> None:
    """E5: a V11.1 tie goes to today's full-form score ("Hello" beats "Hello (Live)")."""
    world = _resolved(monkeypatch, pin_today=False)
    world.v11 = {"Hello": 90.0, "Hello (Live)": 90.0}
    titles = ["Hello", "Hello (Live)"] if plain_first else ["Hello (Live)", "Hello"]
    files = {title: world.file(title) for title in titles}
    song = world.song("Hello")

    world.run()

    assert world.stored(song).match_status == MatchStatus.NEEDS_REVIEW
    assert [f for f, _s, _t in world.rows(song)] == [files["Hello"].id]


def test_tie_on_both_scores_goes_to_the_lower_file_id(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _resolved(monkeypatch, pin_today=False)
    world.v11 = {"Hello": 90.0}
    files = [world.file("Hello"), world.file("Hello")]
    song = world.song("Hello")

    world.run()

    assert [f for f, _s, _t in world.rows(song)] == [min((f.id for f in files), key=str)]


# --- Both real scorers on real titles -------------------------------------------------------------


def test_real_scorers_send_big_log_to_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today auto-matches "Big Log" to "Big Love" (80, a 60-point lead); V11.1 says 64.5."""
    world = _resolved(monkeypatch, pin_today=False)
    big_love = world.file("Big Love")
    world.file("Ship Of Fools")
    song = world.song("Big Log")

    world.run(real_v11=True)

    stored = world.stored(song)
    assert stored.match_status == MatchStatus.NEEDS_REVIEW
    assert stored.reason_code == ReasonCode.TITLE_SCORES_DISAGREE
    [(file_id, score, _tier)] = world.rows(song)
    assert (file_id, round(score, 1)) == (big_love.id, 64.5)


def test_real_scorers_show_heart_beat_at_v11s_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today scores "Heartbeat" vs "Heart Beat" 63; V11.1 auto-matches it at 100: review."""
    world = _resolved(monkeypatch, pin_today=False)
    heart_beat = world.file("Heart Beat")
    world.file("Ship Of Fools")
    song = world.song("Heartbeat")

    world.run(real_v11=True)

    assert world.stored(song).match_status == MatchStatus.NEEDS_REVIEW
    assert world.rows(song) == [(heart_beat.id, 100.0, MatchTier.LOCAL_FILE_FUZZY)]
