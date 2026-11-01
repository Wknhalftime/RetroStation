"""scripts/backfill_live_versions.py caches its searches for the app's lifetime.

The script built its MusicBrainz client without the app's cache lifetime, so
the recording searches it cached on 2026-10-12 got the client's 30-day
default and expire on 2026-11-11, where the app's own entries last ten years.
"""

from __future__ import annotations

import importlib
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import httpx
import pytest

import backend.services.mb_client as mb_mod
from backend.config import Settings
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_MBID = "00000001-0000-4000-8000-000000000000"


@pytest.fixture(scope="module")
def backfill() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("backfill_live_versions")


def _one_live_hit(request: httpx.Request) -> httpx.Response:
    hit = {"id": _MBID, "title": "Dream On", "disambiguation": "live", "score": 100}
    return httpx.Response(200, json={"count": 1, "recordings": [hit]})


def test_cached_searches_last_the_configured_lifetime(
    backfill: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mb_mod, "_rate_limit", lambda: None)
    cache = FakeMusicBrainzCacheRepository()
    # Neither the client's 30-day default nor the app's 3650, so the lifetime
    # can only have come from the settings.
    settings = Settings(mb_cache_ttl_days=1234)

    with backfill.open_mb_client(cache, settings) as client:
        client._http.close()
        client._http = httpx.Client(transport=httpx.MockTransport(_one_live_hit))
        client.search_recordings_by_mbids([_MBID])

    entry = cache.get(f"recording-by-mbid:{_MBID}")
    assert entry is not None
    assert entry.expires_at - entry.cached_at == timedelta(days=1234)
