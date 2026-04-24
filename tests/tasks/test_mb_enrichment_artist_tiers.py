from __future__ import annotations

from unittest.mock import MagicMock

from backend.domain.catalog import Artist, CatalogSource
from backend.tasks.mb_enrichment_tasks import _enhance_artist
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
