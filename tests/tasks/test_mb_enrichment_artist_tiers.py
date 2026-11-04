from __future__ import annotations

from unittest.mock import MagicMock

from backend.domain.catalog import Artist, CatalogSource
from backend.tasks.mb_enrichment_tasks import (
    ArtistEnhanceOutcome,
    _apply_artist_updates,
    _enhance_artist,
    _UnexpectedArtistUpdateColumnError,
)
from tests.fakes.mb_client import FakeMbClient


def _bare_artist(**overrides) -> Artist:
    base = dict(
        id="local-uuid-1",
        name="Unknown Band",
        sort_name="Unknown Band",
        disambiguation=None,
        needs_enhancement=True,
        enhanced_at=None,
        enhancement_error=None,
        mbid=None,
        origin=CatalogSource.LOCAL,
        normalized_name="unknown band",
    )
    base.update(overrides)
    return Artist(**base)


def test_bare_artist_without_mbid_is_quarantined_as_logic_bug():
    """AUD-R008: artists gain an MBID only via release/recording enrichment.

    An MBID-less artist reaching `_enhance_artist` is a logic bug (it should
    never have been queued — `upsert_local_artist` inserts
    `needs_enhancement=FALSE`). It is quarantined via
    `mark_enhancement_failed` instead of being retried (no name search, no
    auto-link) or crashing the phase.
    """
    artist = _bare_artist()
    fake_client = FakeMbClient(
        responses={
            "Unknown Band": [
                {
                    "id": "mb-uuid-123",
                    "score": 99,
                    "sort-name": "Band, Unknown",
                    "disambiguation": "British rock band",
                }
            ]
        }
    )
    conn = MagicMock()
    repos = MagicMock()

    outcome = _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    assert outcome is ArtistEnhanceOutcome.FAILED
    repos.artists.mark_enhancement_failed.assert_called_once_with(
        "local-uuid-1",
        "no MBID: artists gain MBIDs only via release enrichment",
    )
    repos.artists.mark_enhanced.assert_not_called()
    # No name search and no DB write: the bare artist is quarantined
    # immediately, never looked up or auto-linked.
    assert fake_client.calls == []
    conn.execute.assert_not_called()


def _mbid_artist(**overrides) -> Artist:
    base = dict(
        id="local-uuid-2",
        name="Known Band",
        sort_name="Known Band",  # == name → still a "default"
        disambiguation=None,
        needs_enhancement=True,
        enhanced_at=None,
        enhancement_error=None,
        mbid="mb-uuid-known",
        origin=CatalogSource.MUSICBRAINZ,
        normalized_name="known band",
    )
    base.update(overrides)
    return Artist(**base)


def test_tier2_fills_disambiguation_and_sort_name():
    artist = _mbid_artist()
    fake_client = FakeMbClient(
        artists={
            "mb-uuid-known": {
                "id": "mb-uuid-known",
                "name": "Known Band",
                "sort-name": "Band, Known",
                "disambiguation": "US indie rock band",
            }
        }
    )
    conn = MagicMock()
    repos = MagicMock()

    _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    repos.artists.mark_enhanced.assert_called_once_with("local-uuid-2")
    assert conn.execute.called
    args, _ = conn.execute.call_args
    params = args[1]
    assert "US indie rock band" in params
    assert "Band, Known" in params


def test_tier3_all_fields_present_no_update():
    artist = _mbid_artist(disambiguation="Already set", sort_name="Band, Known")
    fake_client = FakeMbClient(
        artists={
            "mb-uuid-known": {
                "id": "mb-uuid-known",
                "name": "Known Band",
                "sort-name": "Band, Known",
                "disambiguation": "Already set",
            }
        }
    )
    conn = MagicMock()
    repos = MagicMock()

    _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    repos.artists.mark_enhanced.assert_called_once_with("local-uuid-2")
    conn.execute.assert_not_called()


def test_tier2_404_marks_enhancement_failed_and_returns_failed_outcome():
    artist = _mbid_artist()
    fake_client = FakeMbClient(artists={})  # lookup returns None
    conn = MagicMock()
    repos = MagicMock()

    outcome = _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    assert outcome is ArtistEnhanceOutcome.FAILED
    repos.artists.mark_enhancement_failed.assert_called_once()
    repos.artists.mark_enhanced.assert_not_called()


def test_mbid_map_short_circuits_api_call():
    artist = _mbid_artist()
    fake_client = FakeMbClient()  # no artists dict entries
    conn = MagicMock()
    repos = MagicMock()
    mbid_map = {
        "mb-uuid-known": {
            "id": "mb-uuid-known",
            "name": "Known Band",
            "sort-name": "Band, Known",
            "disambiguation": "From map",
        }
    }

    _enhance_artist(artist, fake_client, conn, repos, mbid_map=mbid_map)

    assert not any(c.startswith("lookup_artist:") for c in fake_client.calls)
    repos.artists.mark_enhanced.assert_called_once()


def test_mbid_map_with_none_value_treated_as_404():
    """Cached 404 in mbid_map → FAILED without a live lookup_artist call."""
    artist = _mbid_artist()
    fake_client = FakeMbClient()
    conn = MagicMock()
    repos = MagicMock()
    mbid_map: dict[str, object] = {"mb-uuid-known": None}

    outcome = _enhance_artist(
        artist,
        fake_client,
        conn,
        repos,
        mbid_map=mbid_map,  # type: ignore[arg-type]
    )

    assert outcome is ArtistEnhanceOutcome.FAILED
    repos.artists.mark_enhancement_failed.assert_called_once()
    repos.artists.mark_enhanced.assert_not_called()
    assert not any(c.startswith("lookup_artist:") for c in fake_client.calls)


def test_artist_batch_dedups_shared_mbid():
    fake_client = FakeMbClient(artists={"mbid-A": {"id": "mbid-A", "name": "X"}})
    result = fake_client.search_artists_by_mbids(["mbid-A", "mbid-A"])
    assert result["mbid-A"] is not None
    assert fake_client.calls.count("search_artists_by_mbids:mbid-A,mbid-A") == 1


def test_apply_artist_updates_raises_non_retriable_on_bad_column():
    """Allowlist violations are logic bugs; must propagate, not be caught
    by _PER_ITEM_RETRIABLE_ERRORS."""
    import pytest

    conn = MagicMock()
    with pytest.raises(_UnexpectedArtistUpdateColumnError):
        _apply_artist_updates(conn, "local-uuid", {"not_a_column": "value"})


def test_lookup_recordings_dedups_shared_mbid():
    fake_client = FakeMbClient(recordings={"mbid-R": {"id": "mbid-R", "title": "X"}})
    result = fake_client.lookup_recordings(["mbid-R", "mbid-R", "mbid-R"])

    assert result["mbid-R"] is not None
    assert fake_client.calls.count("lookup_recordings:mbid-R,mbid-R,mbid-R") == 1
    assert set(result) == {"mbid-R"}


def test_lookup_recordings_404_stored_as_none():
    # A miss lands in the map as None so the per-item loop knows "already
    # queried, no result" and does NOT re-issue the lookup.
    fake_client = FakeMbClient(recordings={})  # every mbid -> None
    result = fake_client.lookup_recordings(["mbid-missing"])

    assert "mbid-missing" in result
    assert result["mbid-missing"] is None


def test_lookup_recordings_swallows_transient_error_per_mbid():
    """Transient failures on one MBID must not abort the pre-pass.
    The failing MBID is OMITTED from the map (distinct from 404 which lands
    as None) so the per-item loop falls through to its own lookup_recording
    inside the retriable try/except."""
    client = FakeMbClient(
        recordings={"mbid-ok": {"id": "mbid-ok", "title": "title-mbid-ok"}},
        error_mbids={"mbid-flaky"},
    )
    result = client.lookup_recordings(["mbid-ok", "mbid-flaky"])

    assert "mbid-ok" in result
    assert result["mbid-ok"] is not None
    assert "mbid-flaky" not in result  # omitted, distinct from 404-as-None


def test_artist_phase_survives_a_failed_batch_and_still_enhances_each_artist(monkeypatch):
    """A transient failure of the batch search must not fail the phase.

    The phase logs ``mb_artist_batch_failed`` and hands every artist an empty
    map, so each one reaches `_enhance_artist` and takes its own live lookup.
    """
    from contextlib import nullcontext
    from datetime import UTC, datetime

    from structlog.testing import capture_logs

    from backend.tasks import mb_enrichment_tasks as tasks
    from tests.fakes.task_progress import FakeTaskProgressRepository

    fake_client = FakeMbClient(error_mbids={"mbid-1"})
    artists = [_mbid_artist(id=f"a-{i}", mbid=f"mbid-{i}") for i in (1, 2)]
    seen_maps: list[object] = []

    def spy(artist, mb_client, conn, repos, *, mbid_map=None):
        seen_maps.append(mbid_map)
        return ArtistEnhanceOutcome.ENHANCED

    monkeypatch.setattr(tasks, "connect_sync", lambda _url: nullcontext(MagicMock()))
    monkeypatch.setattr(tasks, "PgMusicBrainzCacheRepository", MagicMock())
    monkeypatch.setattr(tasks, "RepositoryFactory", MagicMock())
    monkeypatch.setattr(tasks, "MusicBrainzApiClient", lambda *a, **k: nullcontext(fake_client))
    monkeypatch.setattr(tasks, "_enhance_artist", spy)
    ctx = tasks._PhaseContext(
        task_id="t",
        task_started_at=datetime.now(tz=UTC),
        total=2,
        progress_repo=FakeTaskProgressRepository(),
    )

    with capture_logs() as logs:
        tasks._run_artist_phase(artists, ctx, MagicMock())

    failed = [e for e in logs if e["event"] == "mb_artist_batch_failed"]
    assert len(failed) == 1
    assert failed[0]["distinct"] == 2
    assert seen_maps == [{}, {}]
    assert ctx.done["artists"] == 2
