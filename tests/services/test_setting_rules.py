"""Settings are validated on every save (PR G1, Task 1; traceability A: T1.1-T1.5).

Requirements: D27 ("PR G will validate on save": a listener limit that is not a whole number
>= 1 is refused); PG2 (D27 on every save path; ``stream_sign_off`` is refused by the generic
save, only its upload writes it, D26); house rule: fakes implement the repository ABCs (H6).
"""

from __future__ import annotations

import pytest

from backend.domain.system import InvalidSettingError, ManagedSettingError, SettingsError
from backend.services.setting_rules import save_setting
from tests.fakes.user_settings import FakeUserSettingRepository

LIMIT = "stream_max_sessions"
SIGN_OFF = "stream_sign_off"


def stored(settings: FakeUserSettingRepository, key: str) -> str | None:
    found = settings.get(key)
    return None if found is None else found.value


@pytest.mark.parametrize("value", ["1", "3", "40"])
def test_a_whole_number_of_listeners_is_saved(value: str) -> None:
    # T1.1 (D27): a whole number >= 1 is a valid limit, stored as given.
    settings = FakeUserSettingRepository()
    saved = save_setting(settings, LIMIT, value)
    assert (saved.key, saved.value) == (LIMIT, value)
    assert stored(settings, LIMIT) == value


@pytest.mark.parametrize("value", ["0", "-1", "abc", "2.5", "", " 3", "+3", "1_000", "3\n"])
def test_a_bad_listener_limit_is_refused_and_not_saved(value: str) -> None:
    # T1.2 (D27, PG2): refused with the settings error naming the setting; the stored value
    # is unchanged. "+3", "1_000" and "3\n" are refused because the tune-in refuses them
    # (D27's parse takes decimal digits only; audit SF4).
    settings = FakeUserSettingRepository({LIMIT: "5"})
    with pytest.raises(InvalidSettingError) as refused:
        save_setting(settings, LIMIT, value)
    assert isinstance(refused.value, SettingsError)
    assert LIMIT in str(refused.value)
    assert stored(settings, LIMIT) == "5"


def test_a_setting_without_a_rule_is_saved_as_given() -> None:
    # T1.3 (PG2; guard on the shim): keys with no rule keep today's behaviour.
    settings = FakeUserSettingRepository()
    save_setting(settings, "library.purge_missing", "after_scan")
    save_setting(settings, "theme", "")
    assert stored(settings, "library.purge_missing") == "after_scan"
    assert stored(settings, "theme") == ""


def test_the_sign_off_setting_is_refused_by_the_generic_save() -> None:
    # T1.4 (PG2, D26): only the clip upload writes stream_sign_off.
    settings = FakeUserSettingRepository()
    with pytest.raises(ManagedSettingError) as refused:
        save_setting(settings, SIGN_OFF, '{"file_name": "0123456789abcdef.mp3"}')
    assert isinstance(refused.value, SettingsError)
    assert SIGN_OFF in str(refused.value)
    assert stored(settings, SIGN_OFF) is None


def test_deleting_a_setting_removes_it_and_a_missing_one_is_fine() -> None:
    # T1.5 (H6: the fake implements the ABC's new ``delete``).
    settings = FakeUserSettingRepository({"a": "1", "b": "2"})
    settings.delete("a")
    settings.delete("never-set")
    assert stored(settings, "a") is None
    assert [s.key for s in settings.list_all()] == ["b"]
