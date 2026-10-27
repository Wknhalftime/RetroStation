"""session.liq's processing is the chain the clipping guard passed (R17 on air, Lance
2026-10-27): the same input through session.liq's processing block, after a deck applies its
item gain, and through the harness's render of chain-r17 comes out the same. A doubled item
gain, other values or another chain would not."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, render, signals
from processor_harness.metrics import loudness
from processor_harness.presets import load_preset

from backend.playout.liquidsoap_process import SESSION_SCRIPT, cache_env

LIQUIDSOAP = os.environ.get("LIQUIDSOAP_PATH") or shutil.which("liquidsoap")
REPO = Path(__file__).resolve().parents[3]
R17 = REPO / "scripts" / "processor_harness" / "presets" / "limiter" / "chain-r17.toml"
PARAMS = SESSION_SCRIPT.with_name("chain_params.liq")
FS = 44100
ITEM_GAIN_DB = 6.0
_AFTER_MIX = re.compile(r'^s = add\(id="main_and_bell".*\n', re.MULTILINE)
_PROCESSED = re.compile(r"^s = mksafe\(processed\)\n", re.MULTILINE)


def processing(script: str) -> str:
    """Everything between the decks' mix and the processed source in session.liq."""
    start, end = _AFTER_MIX.search(script), _PROCESSED.search(script)
    assert start and end, "session.liq no longer has the anchors this test splices on"
    return script[start.end() : end.end()]


def session_render(chain: Path, master: Path, out: Path, cache: Path) -> None:
    """A deck carrying an item gain, then session.liq's processing, rendered to ``out``."""
    assert LIQUIDSOAP is not None
    script = out.with_suffix(".liq")
    script.write_text(
        "\n".join(
            [
                "settings.log.level := 2",
                f'%include "{PARAMS.as_posix()}"',
                f'%include "{chain.as_posix()}"',
                "song_started = ref(fun () -> ())",
                f'm = once(single("annotate:liq_amplify=\\"{ITEM_GAIN_DB} dB\\":'
                f'{master.as_posix()}"))',
                's = amplify(1., override="liq_amplify", m)',
                processing(SESSION_SCRIPT.read_text(encoding="utf-8")),
                # The chain's own output ends with the deck; mksafe would go on for ever.
                'clock.assign_new(sync="none", [processed])',
                'o = output.file(%ffmpeg(format="wav", %audio(codec="pcm_f32le")), '
                f'fallible=true, "{out.as_posix()}", processed)',
                "o.on_stop(synchronous=false, fun () -> shutdown())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    run = subprocess.run(
        [LIQUIDSOAP, str(script)], env={**os.environ, **cache_env(cache)},
        capture_output=True, text=True, timeout=240,
    )  # fmt: skip
    assert run.returncode == 0 and out.is_file(), run.stdout[-3000:] + run.stderr[-3000:]


def with_hits(x: np.ndarray) -> np.ndarray:
    """``x`` with a 5 ms burst at -6 dBFS every half second: hits the multiband's 25-33 ms
    attacks let through, so every stage, the limiter included, has work to do."""
    rng = np.random.default_rng(1)
    out = x.copy()
    for start in range(FS // 4, len(x) - FS // 200, FS // 2):
        burst = rng.uniform(-1.0, 1.0, (FS // 200, x.shape[1])) * 10 ** (-6 / 20)
        out[start : start + FS // 200] += burst
    return out


@pytest.mark.slow
@pytest.mark.timeout(300)  # two renders, each with a cold script cache of about 10 s
@pytest.mark.skipif(LIQUIDSOAP is None, reason="LIQUIDSOAP_PATH unset")
def test_session_processing_sounds_exactly_like_the_guarded_chain(tmp_path: Path) -> None:
    assert LIQUIDSOAP is not None
    preset = load_preset(R17)
    assert preset.chain is not None
    x = with_hits(signals.noise_with_pauses(FS, 20.0, 0.0, 1, level_dbfs=-24.0).samples)
    master = tmp_path / "master.wav"
    audio_io.write_wav(master, x, FS)
    gained = tmp_path / "gained.wav"  # what the deck hands on: the master at its item gain
    audio_io.write_wav(gained, x * 10 ** (ITEM_GAIN_DB / 20), FS)

    on_air_chain = SESSION_SCRIPT.with_name("chain.liq")  # what session.liq includes
    session_render(on_air_chain, master, tmp_path / "session.wav", tmp_path / "cache-s")
    render.render_liquidsoap(
        Path(LIQUIDSOAP), preset.chain, preset.params, gained, tmp_path / "harness.wav",
        tmp_path / "cache-h",
    )  # fmt: skip

    on_air, _ = audio_io.read_wav(tmp_path / "session.wav")
    guarded, _ = audio_io.read_wav(tmp_path / "harness.wav")
    n = min(len(on_air), len(guarded))
    assert abs(len(on_air) - len(guarded)) <= FS // 10
    residual = np.sqrt(np.mean((on_air[:n] - guarded[:n]) ** 2))
    assert loudness.to_db(float(residual)) < -60.0, "session.liq processes differently"
    assert loudness.integrated(on_air, FS) == pytest.approx(
        loudness.integrated(guarded, FS), abs=0.05
    )
    # The hits reach the limiter, so its ceiling is part of what must match. (Its sample peaks
    # sit up to about 0.5 dB over -2.5 dBFS on these full-band bursts: the 4x resampler's own
    # ringing, in the harness's render too; R17's measure is decoded true peak on music.)
    assert loudness.to_db(float(np.abs(on_air).max())) > -3.5, "the hits never reached the limiter"
