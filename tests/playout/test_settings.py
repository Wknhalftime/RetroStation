"""Tune-in streaming settings (spec D5: Liquidsoap at a path with no backslash-digit)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import Settings


def test_stream_settings_have_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LIQUIDSOAP_PATH", "STREAM_WORK_DIR", "FFMPEG_PATH"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)
    assert settings.liquidsoap_path is None
    assert settings.stream_work_dir == Path("var/stream")
    assert settings.ffmpeg_path == "ffmpeg"


def test_liquidsoap_path_accepts_existing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    exe = tmp_path / "liquidsoap.exe"
    exe.write_bytes(b"")
    monkeypatch.setenv("LIQUIDSOAP_PATH", str(exe))
    assert Settings(_env_file=None).liquidsoap_path == exe


def test_liquidsoap_path_rejects_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LIQUIDSOAP_PATH", str(tmp_path / "nope.exe"))
    with pytest.raises(ValidationError, match="LIQUIDSOAP_PATH"):
        Settings(_env_file=None)


def test_liquidsoap_path_rejects_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LIQUIDSOAP_PATH", str(tmp_path))
    with pytest.raises(ValidationError, match="LIQUIDSOAP_PATH"):
        Settings(_env_file=None)


@pytest.mark.skipif(sys.platform != "win32", reason="backslash separators are Windows-only")
def test_liquidsoap_path_rejects_backslash_digit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A real file, so only the backslash-digit rule can reject it.
    exe = tmp_path / "2024" / "liquidsoap.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setenv("LIQUIDSOAP_PATH", str(exe))
    with pytest.raises(ValidationError, match="backslash followed by a digit"):
        Settings(_env_file=None)
