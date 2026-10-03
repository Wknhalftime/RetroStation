"""The streaming settings use cases (D10, D27, D34, D42; PG1, PG2, PG6).

``read_stream_settings`` is the one read behind ``GET /api/v1/streaming/settings``: it never
raises on a bad stored value (H9), reporting a problem instead so the page still loads.
``set_max_sessions`` is the one write behind ``PUT /api/v1/streaming/max-sessions``, going
through :func:`~backend.services.setting_rules.save_setting` so D27 applies here exactly as
it does to the generic settings save (PG2).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from backend.domain.streaming import InvalidStreamValueError, SignOff
from backend.repositories.user_settings import UserSettingRepository
from backend.services.setting_rules import save_setting
from backend.services.streaming.errors import InvalidStreamSettingError
from backend.services.streaming.max_sessions import MAX_SESSIONS_KEY, parse_max_sessions
from backend.services.streaming.sign_off import stored_sign_off

MISSING_CLIP_FILE = "the clip's file is missing; upload it again"  # design note 8 (M2)
BAD_SIGN_OFF = "the stored sign-off could not be read; upload the clip again"  # H9


class StreamingState(StrEnum):
    """Whether tune-in streaming is reachable (D34), shown read-only on the settings page."""

    OFF = "off"
    UNAVAILABLE = "unavailable"
    ON = "on"


def streaming_state(*, enabled: bool, running: bool) -> StreamingState:
    """The streaming state (D34, PG1): off when ``STREAM_ENABLED`` is false; unavailable when
    enabled but the engine could not be prepared; on otherwise."""
    if not enabled:
        return StreamingState.OFF
    return StreamingState.ON if running else StreamingState.UNAVAILABLE


@dataclass(frozen=True)
class StreamSettings:
    """The streaming settings page's five keys, read in one pass."""

    streaming: StreamingState
    max_sessions: int | None
    max_sessions_problem: str | None
    sign_off: SignOff | None
    sign_off_problem: str | None


def _read_max_sessions(settings: UserSettingRepository) -> tuple[int | None, str | None]:
    stored = settings.get(MAX_SESSIONS_KEY)
    raw = None if stored is None else stored.value
    try:
        return parse_max_sessions(raw), None
    except InvalidStreamSettingError as bad_value:
        return None, str(bad_value)


def _read_sign_off(
    settings: UserSettingRepository, folder: Path
) -> tuple[SignOff | None, str | None]:
    try:
        sign_off = stored_sign_off(settings)
    except InvalidStreamValueError:
        return None, BAD_SIGN_OFF
    if sign_off is None:
        return None, None
    if not (folder / sign_off.file_name).exists():
        return sign_off, MISSING_CLIP_FILE
    return sign_off, None


def read_stream_settings(
    settings: UserSettingRepository, streaming: StreamingState, folder: Path
) -> StreamSettings:
    """The settings page's five keys (D10, D27, M2; never raises, H9)."""
    max_sessions, max_sessions_problem = _read_max_sessions(settings)
    sign_off, sign_off_problem = _read_sign_off(settings, folder)
    return StreamSettings(streaming, max_sessions, max_sessions_problem, sign_off, sign_off_problem)


def set_max_sessions(settings: UserSettingRepository, value: int) -> int:
    """Validate and store the listener limit (D27; PG6: lowering it stops no one).

    Returns:
        The stored value.
    """
    save_setting(settings, MAX_SESSIONS_KEY, str(value))
    return value
