import re
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_BACKSLASH_DIGIT = re.compile(r"\\\d")


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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

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
