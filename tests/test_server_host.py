"""Where the API listens, and how Liquidsoap reaches it (spec: "The server host becomes a
setting (server_host, default 127.0.0.1; set 0.0.0.0 for the LAN)"; D24; D45: IP literals and
localhost only; audit: an env Settings field, and a non-loopback bind is REFUSED while
airwave_token is "dev-token")."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from backend import run_server
from backend.config import Settings, callback_base_url, is_internal_client

REAL_TOKEN = "a-real-lan-secret"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SERVER_HOST", "SERVER_PORT", "AIRWAVE_TOKEN", "STREAM_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def settings_for(monkeypatch: pytest.MonkeyPatch, host: str, token: str | None = None) -> Settings:
    monkeypatch.setenv("SERVER_HOST", host)
    if token is not None:
        monkeypatch.setenv("AIRWAVE_TOKEN", token)
    return Settings(_env_file=None)


def test_the_api_binds_loopback_port_8010_and_streams_only_when_enabled() -> None:
    settings = Settings(_env_file=None)
    assert (str(settings.server_host), settings.server_port) == ("127.0.0.1", 8010)
    assert settings.stream_enabled is False


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_a_non_loopback_bind_is_refused_with_the_default_token(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    with pytest.raises(ValidationError, match="(?i)server_host") as raised:
        settings_for(monkeypatch, host)
    assert "AIRWAVE_TOKEN" in str(raised.value)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20"])
def test_a_non_loopback_bind_is_allowed_with_a_real_token(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    assert str(settings_for(monkeypatch, host, REAL_TOKEN).server_host) == host


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_bind_keeps_the_dev_token_allowed(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    assert str(settings_for(monkeypatch, host).server_host) == host


def test_a_host_name_other_than_localhost_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # D45: the engine needs one exact callback address, so host names are refused at startup.
    with pytest.raises(ValidationError, match="(?i)server_host"):
        settings_for(monkeypatch, "nas.local", REAL_TOKEN)


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("0.0.0.0", "http://127.0.0.1:8123"),
        ("::", "http://[::1]:8123"),
        ("192.168.1.20", "http://192.168.1.20:8123"),
        ("2001:db8::5", "http://[2001:db8::5]:8123"),
        ("127.0.0.1", "http://127.0.0.1:8123"),
        ("::1", "http://[::1]:8123"),
        ("localhost", "http://localhost:8123"),
    ],
)
def test_the_engine_calls_back_on_the_bound_family(
    monkeypatch: pytest.MonkeyPatch, host: str, url: str
) -> None:
    bound = settings_for(monkeypatch, host, REAL_TOKEN).server_host
    assert callback_base_url(bound, 8123) == url


@pytest.mark.parametrize(
    ("host", "client", "allowed"),
    [
        ("127.0.0.1", "127.0.0.1", True),
        ("0.0.0.0", "127.0.0.1", True),
        ("::", "::1", True),
        ("localhost", "::1", True),
        ("192.168.1.20", "192.168.1.20", True),
        ("192.168.1.20", "192.168.1.77", False),
        ("0.0.0.0", "192.168.1.77", False),
        ("::", "2001:db8::9", False),
    ],
)
def test_internal_clients_follow_the_bind(
    monkeypatch: pytest.MonkeyPatch, host: str, client: str, allowed: bool
) -> None:
    bound = settings_for(monkeypatch, host, REAL_TOKEN).server_host
    assert is_internal_client(client, bound) is allowed


def test_run_server_binds_the_configured_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("SERVER_PORT", "8123")
    monkeypatch.setenv("AIRWAVE_TOKEN", REAL_TOKEN)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(run_server, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(run_server.asyncio, "set_event_loop_policy", lambda policy: None)
    monkeypatch.setattr(run_server.uvicorn, "run", lambda *args, **kw: captured.update(kw))
    run_server.main()
    assert (captured["host"], captured["port"]) == ("0.0.0.0", 8123)
