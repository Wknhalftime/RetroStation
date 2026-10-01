"""LIQUIDSOAP_PATH errors name the honcho backslash-stripping cause when it fits."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import Settings

_STRIPPED_HINT = "looks like the backslashes were removed (honcho reads .env); use forward slashes"


def test_stripped_windows_path_names_the_honcho_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    # honcho turned D:\liquidsoap\liquidsoap-spike.exe into this.
    monkeypatch.setenv("LIQUIDSOAP_PATH", "D:liquidsoapliquidsoap-spike.exe")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "LIQUIDSOAP_PATH (.env): not an existing file" in message
    assert _STRIPPED_HINT in message


def test_missing_path_with_separators_has_no_honcho_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIQUIDSOAP_PATH", (tmp_path / "nope.exe").as_posix())
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    message = str(excinfo.value)
    assert "LIQUIDSOAP_PATH (.env): not an existing file" in message
    assert "backslashes were removed" not in message
