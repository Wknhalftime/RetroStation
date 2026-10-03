"""The stream_max_sessions user setting (D10 "default 3"; D27 "A bad stream_max_sessions (not
a whole number >= 1) refuses tune-ins"; audit: user_settings, one typed parser)."""

from __future__ import annotations

import pytest

from backend.domain.streaming import StreamingError
from backend.services.streaming.errors import InvalidStreamSettingError
from backend.services.streaming.max_sessions import (
    DEFAULT_MAX_SESSIONS,
    MAX_SESSIONS_KEY,
    parse_max_sessions,
)


def test_an_unset_limit_is_three() -> None:
    assert MAX_SESSIONS_KEY == "stream_max_sessions"
    assert DEFAULT_MAX_SESSIONS == 3
    assert parse_max_sessions(None) == 3


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("3", 3), ("12", 12)])
def test_a_whole_number_of_one_or_more_is_the_limit(raw: str, expected: int) -> None:
    assert parse_max_sessions(raw) == expected


@pytest.mark.parametrize("raw", ["0", "-1", "2.5", "three", ""])
def test_anything_else_is_refused_naming_the_setting_and_the_value(raw: str) -> None:
    with pytest.raises(InvalidStreamSettingError, match="stream_max_sessions") as err:
        parse_max_sessions(raw)
    assert repr(raw) in str(err.value)
    assert isinstance(err.value, StreamingError)
