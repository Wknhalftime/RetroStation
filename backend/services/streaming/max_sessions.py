"""The ``stream_max_sessions`` user setting (D10: default 3; D27: anything else is refused)."""

from __future__ import annotations

from backend.services.streaming.errors import InvalidStreamSettingError

MAX_SESSIONS_KEY = "stream_max_sessions"
DEFAULT_MAX_SESSIONS = 3


def parse_max_sessions(raw: str | None) -> int:
    """The listener limit: unset is the default; otherwise a whole number >= 1."""
    if raw is None:
        return DEFAULT_MAX_SESSIONS
    if raw.isdecimal() and int(raw) >= 1:
        return int(raw)
    raise InvalidStreamSettingError(
        f"user_settings.{MAX_SESSIONS_KEY} must be a whole number >= 1, got {raw!r}"
    )
