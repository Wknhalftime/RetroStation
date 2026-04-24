"""Verifies the DEBUG observability events emitted by MusicBrainzApiClient:

- `mb_api_fetch_start` fires before any HTTP call so hangs/timeouts leave a trail.
- `mb_cache_set` fires after every cache write so silent failures are visible.
"""
from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import pytest
import structlog
from structlog.testing import capture_logs

from backend.services.mb_client import MusicBrainzApiClient
from tests.fakes.musicbrainz_cache import FakeMusicBrainzCacheRepository


class _StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _mb_events(captured: list[dict[str, Any]], event_name: str) -> list[dict[str, Any]]:
    return [e for e in captured if e.get("event") == event_name]


@pytest.fixture(autouse=True)
def _ensure_debug_bound_logger() -> Generator[None]:
    """Temporarily set structlog's wrapper_class to a DEBUG-permitting filter.

    `structlog.testing.capture_logs` swaps in a capturing processor list but
    does NOT replace `wrapper_class`. The production config installs
    `make_filtering_bound_logger(INFO)` which drops DEBUG events BEFORE the
    processor chain runs — so `capture_logs` would see nothing. When test
    ordering causes that config to be active (as in CI), the tests below
    silently capture zero events without this fixture.

    Save, swap, restore via try/finally so other tests see the app config.
    """
    original_config = structlog.get_config()
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
    )
    try:
        yield
    finally:
        structlog.configure(**original_config)


def test_mb_api_fetch_start_fires_before_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-fetch log must include the URL and params so a hang is diagnosable."""
    cache = FakeMusicBrainzCacheRepository()

    def stub_get(self: Any, url: str, params: dict[str, str]) -> _StubResponse:  # noqa: ARG001
        return _StubResponse({"id": "mbid-A", "name": "Alice"})

    # Bypass the tenacity retry + rate limiter by patching the low-level http get
    # and the sleep. Patch _rate_limit to a no-op so the test stays fast.
    import backend.services.mb_client as mb_mod
    monkeypatch.setattr(mb_mod, "_rate_limit", lambda: None)

    class _FakeHttp:
        def get(self, url: str, params: dict[str, str]) -> _StubResponse:
            return stub_get(None, url, params)

        def close(self) -> None:
            pass

    def fake_raise_for_status(self: Any) -> None:
        return None

    monkeypatch.setattr(_StubResponse, "raise_for_status", fake_raise_for_status, raising=False)

    with capture_logs() as captured, MusicBrainzApiClient(cache) as client:
        client._http = _FakeHttp()  # type: ignore[assignment]
        client.lookup_artist("mbid-A")

    fetch_events = _mb_events(captured, "mb_api_fetch_start")
    assert len(fetch_events) == 1
    ev = fetch_events[0]
    assert "artist/mbid-A" in ev["url"]
    assert ev["params"] == {"fmt": "json", "inc": "aliases+tags"}
    assert ev["log_level"] == "debug"


def test_mb_cache_set_fires_on_each_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every successful live fetch writes a row; each write emits `mb_cache_set`."""
    cache = FakeMusicBrainzCacheRepository()

    def fake_fetch(self: Any, url: str, params: dict[str, str]) -> _StubResponse:  # noqa: ARG001
        return _StubResponse({"id": "mbid-A", "name": "Alice"})

    monkeypatch.setattr(MusicBrainzApiClient, "_fetch", fake_fetch)

    with capture_logs() as captured, MusicBrainzApiClient(cache) as client:
        client.lookup_artist("mbid-A")

    set_events = _mb_events(captured, "mb_cache_set")
    assert len(set_events) == 1
    ev = set_events[0]
    assert ev["cache_key"] == "artist:mbid-A"
    assert ev["entity_type"] == "artist"
    assert "expires_at" in ev
    assert ev["log_level"] == "debug"


def test_mb_cache_set_not_emitted_on_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second lookup for the same MBID hits cache; no new set should log."""
    cache = FakeMusicBrainzCacheRepository()

    def fake_fetch(self: Any, url: str, params: dict[str, str]) -> _StubResponse:  # noqa: ARG001
        return _StubResponse({"id": "mbid-A", "name": "Alice"})

    monkeypatch.setattr(MusicBrainzApiClient, "_fetch", fake_fetch)

    with capture_logs() as captured, MusicBrainzApiClient(cache) as client:
        client.lookup_artist("mbid-A")  # cold: writes cache
        client.lookup_artist("mbid-A")  # warm: must NOT write again

    set_events = _mb_events(captured, "mb_cache_set")
    assert len(set_events) == 1


