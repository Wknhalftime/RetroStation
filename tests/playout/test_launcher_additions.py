"""PR D1 additions to the session launcher (spec: Engine; items carried from PR A's review).

- EngineStartError is defined in backend.playout (traceability C5).
- A minimal base_env: the child gets what it needs to run and none of the API's secrets (C8).
- long_path: "Paths are sent with the \\\\?\\ prefix"; a UNC path needs \\\\?\\UNC\\ (audit).
- free_port: "Retry once on another port" needs a source of free ports.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

from backend.playout.liquidsoap_process import (
    EngineStartError,
    free_port,
    long_path,
    session_base_env,
)

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows path syntax")
LONG_PREFIX = "\\\\?\\"


def test_engine_start_error_is_defined_in_playout() -> None:
    error = EngineStartError("harbor on 18040 not ready after 5.0 s")
    assert isinstance(error, Exception)
    assert type(error).__module__.startswith("backend.playout.")
    assert "18040" in str(error)


def test_the_session_env_passes_what_the_engine_needs_and_no_secrets() -> None:
    parent = {
        "PATH": r"C:\Windows\system32",
        "SYSTEMROOT": r"C:\Windows",
        "TEMP": r"C:\Temp",
        "TMP": r"C:\Temp",
        "AIRWAVE_TOKEN": "secret",
        "DATABASE_URL": "postgresql://u:p@localhost/db",
        "LIQUIDSOAP_PATH": r"D:\liquidsoap\liquidsoap.exe",
        "RETROSTATION_ANYTHING": "x",
    }
    env = session_base_env(parent)
    for needed in ("PATH", "SYSTEMROOT", "TEMP", "TMP"):
        assert env[needed] == parent[needed]
    for withheld in ("AIRWAVE_TOKEN", "DATABASE_URL", "LIQUIDSOAP_PATH", "RETROSTATION_ANYTHING"):
        assert withheld not in env


def test_free_port_is_a_bindable_unprivileged_loopback_port() -> None:
    port = free_port()
    assert 1024 <= port <= 65535
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


@WINDOWS_ONLY
def test_long_path_prefixes_an_absolute_path_as_given(tmp_path: Path) -> None:
    # Absolute paths are not resolved: resolving a share touches the network. Resolving
    # would also rewrite the folder's case to the on-disk "Music".
    (tmp_path / "Music").mkdir()
    given = Path(str(tmp_path) + r"\MUSIC\a.flac")
    assert long_path(given) == LONG_PREFIX + str(given)


@WINDOWS_ONLY
def test_long_path_uses_the_unc_form_for_a_network_share() -> None:
    assert long_path(Path(r"\\nas\music\Artist\a.flac")) == r"\\?\UNC\nas\music\Artist\a.flac"


@WINDOWS_ONLY
def test_long_path_leaves_an_already_prefixed_path_alone() -> None:
    assert long_path(Path(r"\\?\D:\Music\a.flac")) == r"\\?\D:\Music\a.flac"
