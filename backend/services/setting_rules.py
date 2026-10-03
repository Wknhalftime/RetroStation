"""Generic user-settings validation and write path (D27; PG2).

Every save of a user setting goes through :func:`save_setting`, whatever the caller: the
generic ``PUT /api/v1/settings/{key}`` route and every feature-specific setter (e.g.
``set_max_sessions``). A key with its own write path (``MANAGED``) is refused here; a key
with a validation rule is checked before it is stored; any other key is stored as given.
"""

from __future__ import annotations

from collections.abc import Callable

from backend.domain.system import InvalidSettingError, ManagedSettingError, UserSetting
from backend.repositories.user_settings import UserSettingRepository
from backend.services.streaming.errors import InvalidStreamSettingError
from backend.services.streaming.max_sessions import MAX_SESSIONS_KEY, parse_max_sessions

MANAGED = frozenset({"stream_sign_off"})
"""Keys written only by their own path; the generic save refuses them (PG2, D26). The sign-off
clip's own write path lands in PR G1's Task 3; this is its key, named directly rather than
imported, so the generic settings path has no dependency on the sign-off module."""

type _Validator = Callable[[str], None]
"""Raises a streaming-domain error on a bad value; returns normally on a good one."""


def _check_max_sessions(value: str) -> None:
    parse_max_sessions(value)


_VALIDATORS: dict[str, _Validator] = {
    MAX_SESSIONS_KEY: _check_max_sessions,
}


def save_setting(settings: UserSettingRepository, key: str, value: str) -> UserSetting:
    """Validate and persist one user setting (D27; PG2).

    Args:
        settings: The repository to read and write the setting on.
        key: The setting's key.
        value: The raw value to store.

    Returns:
        The persisted :class:`UserSetting`.

    Raises:
        ManagedSettingError: ``key`` is written only by its own path (PG2).
        InvalidSettingError: ``key`` has a rule and ``value`` fails it (D27), naming the key.
    """
    if key in MANAGED:
        raise ManagedSettingError(f"{key} is managed; it cannot be set directly")
    validate = _VALIDATORS.get(key)
    if validate is not None:
        try:
            validate(value)
        except InvalidStreamSettingError as bad_value:
            raise InvalidSettingError(str(bad_value)) from bad_value
    return settings.upsert(UserSetting(key=key, value=value))
