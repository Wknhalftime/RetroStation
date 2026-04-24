from __future__ import annotations

from unittest.mock import MagicMock

from backend.domain.catalog import Artist, CatalogSource
from backend.tasks.mb_enrichment_tasks import (
    ArtistEnhanceOutcome,
    _apply_artist_updates,
    _enhance_artist,
    _UnexpectedArtistUpdateColumnError,
    coalesce_artist_lookups,
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


def test_tier1_high_confidence_writes_mbid_and_marks_enhanced():
    artist = _bare_artist()
    fake_client = FakeMbClient(
        responses={
            "Unknown Band": [{
                "id": "mb-uuid-123",
                "score": 99,
                "sort-name": "Band, Unknown",
                "disambiguation": "British rock band",
            }]
        }
    )
    conn = MagicMock()
    repos = MagicMock()

    _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    repos.artists.mark_enhanced.assert_called_once_with("local-uuid-1")
    # Verify the UPDATE actually carried the resolved MBID + sort-name +
    # disambiguation — a weak `called` assertion would miss a dropped key.
    conn.execute.assert_called_once()
    params = conn.execute.call_args.args[1]
    assert "mb-uuid-123" in params
    assert "Band, Unknown" in params
    assert "British rock band" in params
    assert "local-uuid-1" in params


def test_tier1_low_confidence_marks_enhanced_without_mbid():
    artist = _bare_artist()
    fake_client = FakeMbClient(
        responses={
            "Unknown Band": [{"id": "mb-uuid-X", "score": 40}]
        }
    )
    conn = MagicMock()
    repos = MagicMock()

    _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    repos.artists.mark_enhanced.assert_called_once_with("local-uuid-1")
    conn.execute.assert_not_called()


def test_tier1_no_results_marks_enhanced():
    artist = _bare_artist()
    fake_client = FakeMbClient(responses={})
    conn = MagicMock()
    repos = MagicMock()

    _enhance_artist(artist, fake_client, conn, repos, mbid_map=None)

    repos.artists.mark_enhanced.assert_called_once_with("local-uuid-1")
    conn.execute.assert_not_called()


def _mbid_artist(**overrides) -> Artist:
    base = dict(
        id="local-uuid-2",
        name="Known Band",
        sort_name="Known Band",           # == name → still a "default"
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
        artists={"mb-uuid-known": {
            "id": "mb-uuid-known",
            "name": "Known Band",
            "sort-name": "Band, Known",
            "disambiguation": "US indie rock band",
        }}
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
        artists={"mb-uuid-known": {
            "id": "mb-uuid-known",
            "name": "Known Band",
            "sort-name": "Band, Known",
            "disambiguation": "Already set",
        }}
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
        artist, fake_client, conn, repos, mbid_map=mbid_map,  # type: ignore[arg-type]
    )

    assert outcome is ArtistEnhanceOutcome.FAILED
    repos.artists.mark_enhancement_failed.assert_called_once()
    repos.artists.mark_enhanced.assert_not_called()
    assert not any(c.startswith("lookup_artist:") for c in fake_client.calls)


def test_coalesce_artist_lookups_dedups_shared_mbid():
    fake_client = FakeMbClient(artists={"mbid-A": {"id": "mbid-A", "name": "X"}})
    result = coalesce_artist_lookups({"mbid-A"}, fake_client)
    assert result["mbid-A"] is not None
    assert fake_client.calls.count("lookup_artist:mbid-A") == 1


def test_coalesce_artist_lookups_list_with_duplicates():
    fake_client = FakeMbClient(artists={"mbid-A": {"id": "mbid-A", "name": "X"}})
    result = coalesce_artist_lookups(["mbid-A", "mbid-A", "mbid-A"], fake_client)
    assert fake_client.calls.count("lookup_artist:mbid-A") == 1
    assert set(result) == {"mbid-A"}


def test_apply_artist_updates_raises_non_retriable_on_bad_column():
    """Allowlist violations are logic bugs; must propagate, not be caught
    by _PER_ITEM_RETRIABLE_ERRORS."""
    import pytest

    conn = MagicMock()
    with pytest.raises(_UnexpectedArtistUpdateColumnError):
        _apply_artist_updates(conn, "local-uuid", {"not_a_column": "value"})


def test_coalesce_artist_lookups_swallows_transient_error_per_mbid():
    """A transient httpx failure on one MBID must NOT abort the pre-pass.

    Failing MBIDs are omitted from the map so the per-item loop falls
    through to its own lookup (inside the retriable try/except), turning
    a pre-pass failure into a per-item failure instead of a task abort.
    """
    import httpx

    class FlakyClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def lookup_artist(self, mbid: str):
            self.calls.append(f"lookup_artist:{mbid}")
            if mbid == "mbid-flaky":
                raise httpx.ConnectError("boom")
            return {"id": mbid, "name": f"name-{mbid}"}

        # Protocol fills — unused by this test
        def search_artist(self, name: str):
            return []
        def lookup_release(self, mbid: str):
            return None
        def lookup_recording(self, mbid: str):
            return None
        def search_recording(self, artist_mbid: str, title: str, limit: int = 10):
            return []

    client = FlakyClient()
    result = coalesce_artist_lookups({"mbid-ok", "mbid-flaky"}, client)

    assert "mbid-ok" in result
    assert result["mbid-ok"] is not None
    assert "mbid-flaky" not in result  # omitted, not present-with-None
