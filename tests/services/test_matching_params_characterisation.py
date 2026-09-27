"""Characterisation snapshots for AUD-015 / AUD-040 (gate 1).

Locks TODAY's persisted state, returned work_ids, catalog upserts and MB
client call sequence for both ``match_identities_for_playlist`` and
``match_artists_for_playlist`` before their loose-parameter signatures are
grouped into frozen repos/threshold dataclasses (see
``.claude/rules/refactoring-workflow.md`` — more than 4 params means a
config object).

In the refactor commit, only the two ``match_*_for_playlist(...)`` call
sites below may change (positional/keyword repo args -> repos objects).
Every captured value must stay byte-identical: all fixture ids are fixed
(``_uid``), never ``uuid4()``, so re-running this file never perturbs the
snapshot on its own.

Scenario coverage (per the audit plan):
  match_artists_for_playlist  - mapping rule, normalization exact against an
    MBID-bearing canonical, normalization exact against a local-only
    canonical (target_id = local id), truncated-name MB auto-match,
    truncated-name MB ambiguous-gap, truncated-name MB low-confidence,
    non-truncated deferred-retry, and the AUTO_REJECTED / DEFERRED_RETRY
    identity cascades.
  match_identities_for_playlist - Tier 0 mapping rule, Tier 1 Step A (local
    MBID lookup) auto-match / ambiguous-gap / low-confidence, Tier 1 Step B
    (MB recording search), Tier 1 Step C (name fallback for a local-only
    canonical target), Tier 2 fuzzy fallback for an unresolved artist,
    MISSING_MATCH_RECORD, NO_LOCAL_FILES, and an orphaned identity.
"""

from __future__ import annotations

from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Artist
from backend.domain.enums import CatalogSource, MatchStatus, MatchTier, TargetType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import MappingRule, Match
from backend.services.artist_matching_service import match_artists_for_playlist
from backend.services.identity_matching_service import match_identities_for_playlist
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


def _uid(n: int) -> UUID:
    """Deterministic UUID so fixture ids (and thus the snapshot) never drift."""
    return UUID(int=n)


def _truncated_name(seed: str, length: int = 29) -> str:
    """A name >= broadcast_name_max_len - tolerance chars long, alnum-terminated."""
    text = (seed * 10)[:length]
    if not text[-1].isalnum():
        text = text[:-1] + "x"
    return text


def _lib_file(
    file_id: UUID,
    path: str,
    *,
    artist_mbid: str | None = None,
    recording_mbid: str | None = None,
    track_title: str | None = None,
    artist_name: str = "Metallica",
    normalized_artist_name: str = "metallica",
    work_id: str | None = None,
    recording_id: str | None = None,
) -> LibraryFile:
    normalized_title = normalize_title(track_title) if track_title else None
    return LibraryFile(
        id=file_id,
        file_path=path,
        file_hash="hash-" + path,
        format="flac",
        recording_id=recording_id,
        work_id=work_id,
        audio=AudioMetadata(
            artist_mbid=artist_mbid,
            recording_mbid=recording_mbid,
            track_title=track_title,
            normalized_title=normalized_title,
            artist_name=artist_name,
            normalized_artist_name=normalized_artist_name,
        ),
    )


def _identity(
    identity_id: UUID,
    artist_id: UUID,
    title: str,
    artist_normalized_name: str,
) -> BroadcastTrackIdentity:
    norm_title = normalize_title(title)
    norm_sig = compute_normalized_signature(artist_normalized_name, norm_title)
    return BroadcastTrackIdentity(
        id=identity_id,
        broadcast_artist_id=artist_id,
        original_title=title,
        normalized_title=norm_title,
        normalized_signature=norm_sig,
    )


# ---------------------------------------------------------------------------
# match_artists_for_playlist
# ---------------------------------------------------------------------------


def test_match_artists_for_playlist_full_scenario_snapshot(snapshot: SnapshotAssertion) -> None:
    playlist_id = _uid(1000)

    broadcast_artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    rules_repo = FakeMappingRuleRepository()

    truncated_auto_name = _truncated_name("Zephyr Junction Broadcast ")
    truncated_ambiguous_name = _truncated_name("Wandering Prairie Skyline ")
    truncated_lowconf_name = _truncated_name("Copper Harbor Station Feed ")

    mb_client = FakeMbClient(
        responses={
            truncated_auto_name: [
                {"id": "mb-truncated-auto", "score": 97, "name": truncated_auto_name},
            ],
            truncated_ambiguous_name: [
                {"id": "mb-ambig-a", "score": 85, "name": truncated_ambiguous_name},
                {"id": "mb-ambig-b", "score": 78, "name": truncated_ambiguous_name},
            ],
            truncated_lowconf_name: [
                {"id": "mb-lowconf", "score": 62, "name": truncated_lowconf_name},
            ],
        }
    )

    artist_repo = FakeArtistRepository()
    metallica_mbid = "mbid-metallica-canonical"
    artist_repo.upsert(
        Artist(
            id=metallica_mbid,
            name="Metallica",
            sort_name="Metallica",
            mbid=metallica_mbid,
            origin=CatalogSource.MUSICBRAINZ,
            normalized_name=normalize_artist("Metallica"),
            needs_enhancement=False,
        )
    )
    local_combo_id = "local-combo-catalog-id"
    artist_repo.upsert(
        Artist(
            id=local_combo_id,
            name="Local Combo",
            sort_name="Local Combo",
            mbid=None,
            origin=CatalogSource.LOCAL,
            normalized_name=normalize_artist("Local Combo"),
            needs_enhancement=False,
        )
    )

    def _register(
        idx: int, name: str, status: MatchStatus = MatchStatus.PENDING
    ) -> BroadcastArtist:
        artist = BroadcastArtist(
            id=_uid(idx),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=status,
        )
        broadcast_artist_repo.upsert(artist)
        broadcast_artist_repo.register_playlist_artist(playlist_id, artist.id)
        return artist

    rule_artist = _register(1, "Rule Band")
    norm_mbid_artist = _register(2, "Metallica")
    norm_local_artist = _register(3, "Local Combo")
    truncated_auto_artist = _register(4, truncated_auto_name)
    truncated_ambiguous_artist = _register(5, truncated_ambiguous_name)
    truncated_lowconf_artist = _register(6, truncated_lowconf_name)
    deferred_artist = _register(7, "New Combo")
    rejected_artist = _register(8, "Rejected Combo", status=MatchStatus.AUTO_REJECTED)

    rules_repo.create(
        MappingRule(
            id=_uid(101),
            source_pattern=rule_artist.normalized_name,
            target_type=TargetType.ARTIST,
            target_id="mbid-ruleband",
            priority=10,
        )
    )

    deferred_identity = _identity(
        _uid(201), deferred_artist.id, "Some Track", deferred_artist.normalized_name
    )
    identity_repo.upsert(deferred_identity)
    identity_repo.register_playlist_identity(playlist_id, deferred_identity.id)

    rejected_identity = _identity(
        _uid(202), rejected_artist.id, "Other Track", rejected_artist.normalized_name
    )
    identity_repo.upsert(rejected_identity)
    identity_repo.register_playlist_identity(playlist_id, rejected_identity.id)

    match_artists_for_playlist(
        playlist_id=playlist_id,
        broadcast_artist_repo=broadcast_artist_repo,
        track_identity_repo=identity_repo,
        artist_repo=artist_repo,
        match_repo=match_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
    )

    def _artist_state(artist_id: UUID) -> dict[str, object]:
        stored = broadcast_artist_repo.get_by_id(artist_id)
        assert stored is not None
        match = match_repo.get_by_artist(artist_id)
        return {
            "match_status": stored.match_status.value,
            "reason_code": stored.reason_code.value if stored.reason_code else None,
            "reason_detail": stored.reason_detail,
            "match": (
                {
                    "target_id": match.target_id,
                    "target_type": match.target_type.value if match.target_type else None,
                    "confidence_score": match.confidence_score,
                    "match_tier": match.match_tier.value,
                }
                if match is not None
                else None
            ),
        }

    def _identity_state(identity_id: UUID) -> dict[str, object]:
        stored = identity_repo.get_by_id(identity_id)
        assert stored is not None
        return {
            "match_status": stored.match_status.value,
            "reason_code": stored.reason_code.value if stored.reason_code else None,
        }

    result = {
        "rule_artist": _artist_state(rule_artist.id),
        "norm_exact_mbid_artist": _artist_state(norm_mbid_artist.id),
        "norm_exact_local_artist": _artist_state(norm_local_artist.id),
        "truncated_auto_artist": _artist_state(truncated_auto_artist.id),
        "truncated_ambiguous_artist": _artist_state(truncated_ambiguous_artist.id),
        "truncated_lowconf_artist": _artist_state(truncated_lowconf_artist.id),
        "deferred_artist": _artist_state(deferred_artist.id),
        "rejected_artist_unchanged": _artist_state(rejected_artist.id),
        "deferred_identity": _identity_state(deferred_identity.id),
        "rejected_identity": _identity_state(rejected_identity.id),
        "musicbrainz_upserts": artist_repo.musicbrainz_upserts,
        "mb_client_calls": sorted(mb_client.calls),
    }

    assert result == snapshot


# ---------------------------------------------------------------------------
# match_identities_for_playlist
# ---------------------------------------------------------------------------


def test_match_identities_for_playlist_full_scenario_snapshot(
    snapshot: SnapshotAssertion,
) -> None:
    playlist_id = _uid(2000)

    broadcast_artist_repo = FakeBroadcastArtistRepository()
    identity_repo = FakeBroadcastTrackIdentityRepository()
    match_repo = FakeMatchRepository()
    lib_repo = FakeLibraryFileRepository()
    rules_repo = FakeMappingRuleRepository()
    catalog_repo = FakeArtistRepository()

    def _register_artist(
        idx: int, name: str, status: MatchStatus = MatchStatus.AUTO_MATCHED
    ) -> BroadcastArtist:
        artist = BroadcastArtist(
            id=_uid(idx),
            original_name=name,
            normalized_name=normalize_artist(name),
            match_status=status,
        )
        broadcast_artist_repo.upsert(artist)
        return artist

    def _resolve(artist: BroadcastArtist, target_id: str) -> None:
        match_repo.create(
            Match(
                id=_uid(90000 + artist.id.int),
                artist_id=artist.id,
                target_id=target_id,
                target_type=TargetType.ARTIST,
                confidence_score=100.0,
                match_tier=MatchTier.MUSICBRAINZ_ID_EXACT,
            )
        )

    # -- Tier 0: mapping rule -------------------------------------------------
    rule_artist = _register_artist(1, "Rule Artist Band", status=MatchStatus.PENDING)
    rule_lib_file = _lib_file(
        _uid(11),
        "/music/rule/track.flac",
        track_title="Rule Track",
        artist_name=rule_artist.original_name,
        normalized_artist_name=rule_artist.normalized_name,
    )
    lib_repo.upsert(rule_lib_file)
    rule_identity = _identity(_uid(21), rule_artist.id, "Rule Track", rule_artist.normalized_name)
    identity_repo.upsert(rule_identity)
    identity_repo.register_playlist_identity(playlist_id, rule_identity.id)
    rules_repo.create(
        MappingRule(
            id=_uid(31),
            source_pattern=rule_identity.normalized_signature,
            target_type=TargetType.LIBRARY_FILE,
            target_id=str(rule_lib_file.id),
            priority=10,
        )
    )

    # -- Tier 1 Step A: auto-match --------------------------------------------
    step_a_auto_artist = _register_artist(2, "Step A Auto Band")
    _resolve(step_a_auto_artist, "mbid-step-a-auto")
    step_a_auto_file = _lib_file(
        _uid(12),
        "/music/step_a_auto/master.flac",
        artist_mbid="mbid-step-a-auto",
        track_title="Master Of Puppets",
        artist_name=step_a_auto_artist.original_name,
        normalized_artist_name=step_a_auto_artist.normalized_name,
        work_id="work-master-of-puppets",
    )
    lib_repo.upsert(step_a_auto_file)
    step_a_auto_identity = _identity(
        _uid(22), step_a_auto_artist.id, "Master Of Puppets", step_a_auto_artist.normalized_name
    )
    identity_repo.upsert(step_a_auto_identity)
    identity_repo.register_playlist_identity(playlist_id, step_a_auto_identity.id)

    # -- Tier 1 Step A: ambiguous gap (two close local candidates) -----------
    step_a_ambiguous_artist = _register_artist(3, "Step A Ambiguous Band")
    _resolve(step_a_ambiguous_artist, "mbid-step-a-ambiguous")
    ambig_file_a = _lib_file(
        _uid(13),
        "/music/step_a_ambiguous/a.flac",
        artist_mbid="mbid-step-a-ambiguous",
        track_title="The Sandman Enters",
        artist_name=step_a_ambiguous_artist.original_name,
        normalized_artist_name=step_a_ambiguous_artist.normalized_name,
    )
    ambig_file_b = _lib_file(
        _uid(14),
        "/music/step_a_ambiguous/b.flac",
        artist_mbid="mbid-step-a-ambiguous",
        track_title="Enter Sandman Deluxe",
        artist_name=step_a_ambiguous_artist.original_name,
        normalized_artist_name=step_a_ambiguous_artist.normalized_name,
    )
    lib_repo.upsert(ambig_file_a)
    lib_repo.upsert(ambig_file_b)
    step_a_ambiguous_identity = _identity(
        _uid(23),
        step_a_ambiguous_artist.id,
        "Enter Sandman",
        step_a_ambiguous_artist.normalized_name,
    )
    identity_repo.upsert(step_a_ambiguous_identity)
    identity_repo.register_playlist_identity(playlist_id, step_a_ambiguous_identity.id)

    # -- Tier 1 Step A: low confidence (single weak local candidate) ---------
    step_a_lowconf_artist = _register_artist(4, "Step A Lowconf Band")
    _resolve(step_a_lowconf_artist, "mbid-step-a-lowconf")
    lowconf_file = _lib_file(
        _uid(15),
        "/music/step_a_lowconf/x.flac",
        artist_mbid="mbid-step-a-lowconf",
        track_title="Sandman Anthem Live Version",
        artist_name=step_a_lowconf_artist.original_name,
        normalized_artist_name=step_a_lowconf_artist.normalized_name,
    )
    lib_repo.upsert(lowconf_file)
    step_a_lowconf_identity = _identity(
        _uid(24), step_a_lowconf_artist.id, "Enter Sandman", step_a_lowconf_artist.normalized_name
    )
    identity_repo.upsert(step_a_lowconf_identity)
    identity_repo.register_playlist_identity(playlist_id, step_a_lowconf_identity.id)

    # -- Tier 1 Step B: MB recording search ------------------------------------
    step_b_artist = _register_artist(5, "Step B Band")
    _resolve(step_b_artist, "mbid-step-b")
    step_b_identity = _identity(_uid(25), step_b_artist.id, "One", step_b_artist.normalized_name)
    identity_repo.upsert(step_b_identity)
    identity_repo.register_playlist_identity(playlist_id, step_b_identity.id)
    step_b_search_title = broadcast_title_variants(step_b_identity.original_title)[0]
    step_b_file = _lib_file(
        _uid(16),
        "/music/step_b/one.flac",
        recording_mbid="rec-step-b",
        track_title="One",
        artist_name=step_b_artist.original_name,
        normalized_artist_name=step_b_artist.normalized_name,
    )
    lib_repo.upsert(step_b_file)
    mb_client = FakeMbClient(
        recording_searches={
            ("mbid-step-b", step_b_search_title): [{"id": "rec-step-b"}],
        }
    )

    # -- Tier 1 Step C: name fallback (local-only canonical target) ----------
    step_c_artist = _register_artist(6, "Step C Band")
    step_c_local_catalog_id = "local-catalog-step-c"
    catalog_repo.upsert(
        Artist(
            id=step_c_local_catalog_id,
            name=step_c_artist.original_name,
            sort_name=step_c_artist.original_name,
            mbid=None,
            origin=CatalogSource.LOCAL,
            normalized_name=step_c_artist.normalized_name,
            needs_enhancement=False,
        )
    )
    _resolve(step_c_artist, step_c_local_catalog_id)
    step_c_file = _lib_file(
        _uid(17),
        "/music/step_c/song.flac",
        track_title="Step C Song",
        artist_name=step_c_artist.original_name,
        normalized_artist_name=step_c_artist.normalized_name,
    )
    lib_repo.upsert(step_c_file)
    step_c_identity = _identity(
        _uid(26), step_c_artist.id, "Step C Song", step_c_artist.normalized_name
    )
    identity_repo.upsert(step_c_identity)
    identity_repo.register_playlist_identity(playlist_id, step_c_identity.id)

    # -- Tier 2: fuzzy fallback for an unresolved artist ----------------------
    tier2_artist = _register_artist(7, "Tier2 Band", status=MatchStatus.PENDING)
    tier2_file = _lib_file(
        _uid(18),
        "/music/tier2/x.flac",
        track_title="Sandman Anthem Live Version",
        artist_name=tier2_artist.original_name,
        normalized_artist_name=tier2_artist.normalized_name,
    )
    lib_repo.upsert(tier2_file)
    tier2_identity = _identity(
        _uid(27), tier2_artist.id, "Enter Sandman", tier2_artist.normalized_name
    )
    identity_repo.upsert(tier2_identity)
    identity_repo.register_playlist_identity(playlist_id, tier2_identity.id)

    # -- NO_LOCAL_FILES: resolved artist, nothing anywhere matches -----------
    no_candidates_artist = _register_artist(8, "Ghost Band")
    _resolve(no_candidates_artist, "mbid-no-candidates")
    no_candidates_identity = _identity(
        _uid(28), no_candidates_artist.id, "Nonexistent Song", no_candidates_artist.normalized_name
    )
    identity_repo.upsert(no_candidates_identity)
    identity_repo.register_playlist_identity(playlist_id, no_candidates_identity.id)

    # -- MISSING_MATCH_RECORD: resolved status but no match row --------------
    missing_record_artist = _register_artist(9, "Missing Record Band")
    missing_record_identity = _identity(
        _uid(29),
        missing_record_artist.id,
        "Some Song",
        missing_record_artist.normalized_name,
    )
    identity_repo.upsert(missing_record_identity)
    identity_repo.register_playlist_identity(playlist_id, missing_record_identity.id)

    # -- Orphaned identity: no BroadcastArtist row at all ---------------------
    orphaned_identity = _identity(_uid(30), _uid(9999), "Orphan Song", "orphan band")
    identity_repo.upsert(orphaned_identity)
    identity_repo.register_playlist_identity(playlist_id, orphaned_identity.id)

    work_ids = match_identities_for_playlist(
        playlist_id=playlist_id,
        track_identity_repo=identity_repo,
        broadcast_artist_repo=broadcast_artist_repo,
        match_repo=match_repo,
        library_file_repo=lib_repo,
        rules_repo=rules_repo,
        mb_client=mb_client,
        catalog_repo=catalog_repo,
    )

    def _identity_state(identity_id: UUID) -> dict[str, object]:
        stored = identity_repo.get_by_id(identity_id)
        assert stored is not None
        persisted = match_repo.get_by_identity(identity_id)
        return {
            "match_status": stored.match_status.value,
            "match_tier": stored.match_tier.value if stored.match_tier else None,
            "reason_code": stored.reason_code.value if stored.reason_code else None,
            "reason_detail": stored.reason_detail,
            "match": (
                {
                    "library_file_id": str(persisted.library_file_id),
                    "confidence_score": persisted.confidence_score,
                    "match_tier": persisted.match_tier.value,
                    "work_id": persisted.work_id,
                }
                if persisted is not None
                else None
            ),
        }

    result = {
        "work_ids": sorted(work_ids),
        "rule_identity": _identity_state(rule_identity.id),
        "step_a_auto_identity": _identity_state(step_a_auto_identity.id),
        "step_a_ambiguous_identity": _identity_state(step_a_ambiguous_identity.id),
        "step_a_lowconf_identity": _identity_state(step_a_lowconf_identity.id),
        "step_b_identity": _identity_state(step_b_identity.id),
        "step_c_identity": _identity_state(step_c_identity.id),
        "tier2_identity": _identity_state(tier2_identity.id),
        "no_candidates_identity": _identity_state(no_candidates_identity.id),
        "missing_record_identity": _identity_state(missing_record_identity.id),
        "orphaned_identity": _identity_state(orphaned_identity.id),
        "mb_client_calls": sorted(mb_client.calls),
    }

    assert result == snapshot
