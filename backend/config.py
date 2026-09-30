import re
from functools import lru_cache
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKSLASH_DIGIT = re.compile(r"\\\d")
_DEV_TOKEN = "dev-token"
_EXAMPLE_TOKEN = "change-me-before-use"  # the placeholder in .env.example
_PLACEHOLDER_TOKENS = frozenset({_DEV_TOKEN, _EXAMPLE_TOKEN})
_LOOPBACK_CLIENTS = ("127.0.0.1", "::1")

type BindHost = IPv4Address | IPv6Address | Literal["localhost"]
"""Where the API listens: an address, or ``localhost`` (D24, D45). A host name is refused."""


def _is_loopback(host: BindHost) -> bool:
    return host == "localhost" or host.is_loopback


def callback_base_url(host: BindHost, port: int) -> str:
    """The URL a session engine calls back on, in the bound address family (D24).

    A wildcard bind is reached on that family's loopback: ``0.0.0.0`` on 127.0.0.1 and
    ``::`` on ``[::1]``.
    """
    if host == "localhost":
        return f"http://localhost:{port}"
    if isinstance(host, IPv4Address):
        return f"http://{'127.0.0.1' if host.is_unspecified else host}:{port}"
    return f"http://[{'::1' if host.is_unspecified else host}]:{port}"


def is_internal_client(client_host: str, server_host: BindHost) -> bool:
    """Whether a request comes from this machine: loopback, or the bound address itself."""
    if client_host in _LOOPBACK_CLIENTS:
        return True
    specific = server_host != "localhost" and not server_host.is_unspecified
    return specific and client_host == str(server_host)


class Settings(BaseSettings):
    database_url: str = "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation"
    airwave_token: str = "dev-token"
    log_level: str = "INFO"
    mb_auto_link_score: int = 95
    mb_score_gap: int = 10
    # MusicBrainz payloads for a given MBID do not change; refresh by clearing mb_cache.
    mb_cache_ttl_days: int = 3650
    strong_match_threshold: int = 80
    min_presentation_score: int = 50
    broadcast_name_max_len: int = 30
    library_scan_paths: list[str] = []
    # Tune-in streaming (docs/superpowers/specs/2026-09-27-tune-in-streaming-design.md).
    # None disables streaming.
    liquidsoap_path: Path | None = None
    # Generated filler/intro audio, per-session logs and Liquidsoap's script cache.
    stream_work_dir: Path = Path("var/stream")
    ffmpeg_path: str = "ffmpeg"
    # Where the API listens (D24): 0.0.0.0 or a LAN address serves the LAN.
    server_host: BindHost = IPv4Address("127.0.0.1")
    server_port: int = 8010
    # Streaming is opt-in: it warms Liquidsoap's cache and prunes logs at startup.
    stream_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @field_validator("server_port")
    @classmethod
    def _server_port_in_range(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError(f"SERVER_PORT (.env): must be 1..65535, got {value}")
        return value

    @model_validator(mode="after")
    def _lan_bind_needs_a_real_token(self) -> Self:
        if not _is_loopback(self.server_host) and self.airwave_token in _PLACEHOLDER_TOKENS:
            raise ValueError(
                f"SERVER_HOST (.env): binding {self.server_host} exposes the API beyond this "
                "machine; set AIRWAVE_TOKEN (.env) to a secret first"
            )
        return self

    @field_validator("liquidsoap_path")
    @classmethod
    def _liquidsoap_path_usable(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if _BACKSLASH_DIGIT.search(str(value)):
            raise ValueError(
                f"LIQUIDSOAP_PATH (.env): {value} contains a backslash followed by a digit; "
                "Liquidsoap 2.4.5 crashes at startup from such a path"
            )
        if not value.is_file():
            raise ValueError(f"LIQUIDSOAP_PATH (.env): not an existing file: {value}")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
