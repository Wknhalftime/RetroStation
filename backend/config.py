from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql://retrostation:retrostation-dev@localhost:5432/retrostation"
    airwave_token: str = "dev-token"
    log_level: str = "INFO"
    mb_auto_link_score: int = 95
    mb_score_gap: int = 10
    mb_needs_review_threshold: int = 50
    library_scan_paths: list[str] = []

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
