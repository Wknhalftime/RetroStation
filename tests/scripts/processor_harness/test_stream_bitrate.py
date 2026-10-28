"""The stream's MP3 bitrate is 256k (R20; Lance, 2026-10-28: 128k made the highs sound
"pixelated"), and everything the harness encodes or judges uses the stream's bitrate: the
renders' MP3s, the host load test's sessions and the clipping guard's encoder states."""

from __future__ import annotations

import re
from pathlib import Path

from processor_harness import codec, host_load, render
from processor_harness.presets import load_preset

from backend.playout.liquidsoap_process import SESSION_SCRIPT

PRESETS = Path(__file__).resolve().parents[3] / "scripts" / "processor_harness" / "presets"
_HARBOR_MP3 = re.compile(r'%audio\(codec = "libmp3lame", b = "([^"]+)", ar = 44100\)')


def session_bitrate() -> str:
    """The bitrate session.liq's harbor output encodes at."""
    found = _HARBOR_MP3.findall(SESSION_SCRIPT.read_text(encoding="utf-8"))
    assert len(found) == 1, f"expected one MP3 encoder in session.liq, found {found}"
    return str(found[0])


def test_the_stream_encodes_mp3_at_256k() -> None:
    assert session_bitrate() == "256k"


def test_the_harness_encodes_at_the_streams_bitrate() -> None:
    assert session_bitrate() == codec.MP3_BITRATE


def test_a_render_writes_its_mp3_at_the_streams_bitrate(tmp_path: Path) -> None:
    script = render.render_script(
        chain=tmp_path / "chain.liq",
        params={},
        source=tmp_path / "in.wav",
        wav=tmp_path / "out.wav",
        mp3=tmp_path / "out.mp3",
    )

    assert f'codec="libmp3lame", b="{session_bitrate()}", ar=44100' in script


def test_a_host_load_session_encodes_at_the_streams_bitrate(tmp_path: Path) -> None:
    preset = load_preset(PRESETS / "limiter" / "chain-r17.toml")

    text = host_load.write_session_script(preset, tmp_path / "session.liq").read_text("utf-8")

    assert f'codec="libmp3lame", b="{session_bitrate()}", ar=44100' in text
