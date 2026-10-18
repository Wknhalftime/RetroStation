"""session.liq's AGC never re-applies an item's liq_amplify (real Liquidsoap 2.4.5, offline).

On 2.4.5 the stdlib ``normalize`` ends with ``amplify({v()}, ...)``, whose ``override``
defaults to ``liq_amplify``. A track boundary carrying it replaces the AGC gain with the
item gain: the gain lands twice and the AGC stops riding (measured -28.5 LUFS instead of
-16 on Killing in the Name). In session.liq the decks' ``add()`` happens to hide those
boundaries; this test feeds a deck straight into what session.liq does after that point,
so the guard has to hold on its own.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from backend.playout.liquidsoap_process import SESSION_SCRIPT, cache_env

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

ITEM_GAIN_DB = -10.0  # the incident's item: a -7 LUFS master at -10.5 dB
MASTER_LUFS = -8.0  # measured: 500 Hz sine at the default 1/8 amplitude, raised 13.7 dB
SPAN_S = 20

# From the line after the mix is built to the end of the AGC call: everything that sits
# between the decks and normalize in session.liq.
_AFTER_MIX = re.compile(r'^s = add\(id="main_and_bell".*\n', re.MULTILINE)
_AGC_CALL = re.compile(r"^s = normalize\(.*?^\)\n", re.MULTILINE | re.DOTALL)


def up_to_agc(script: str) -> str:
    start, call = _AFTER_MIX.search(script), _AGC_CALL.search(script)
    assert start and call, "session.liq no longer has the anchors this test splices on"
    assert start.end() <= call.start(), "the AGC call moved before the mix"
    return script[start.end() : call.end()]


def loud_master(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=500:duration={SPAN_S}:sample_rate=44100",
            "-af",
            "volume=13.7dB",
            "-ac",
            "2",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


def render_script(agc: str) -> str:
    """One deck (as mkdeck builds it) carrying liq_amplify, then session.liq's AGC."""
    return "\n".join(
        [
            "settings.log.level := 2",
            'm = single("annotate:liq_amplify=\\"#{environment.get(\'ITEM_GAIN\')} dB\\":'
            "#{environment.get('MASTER')}\")",
            's = amplify(1., override="liq_amplify", m)',
            agc,
            "s = once(s)",
            'clock.assign_new(sync="none", [s])',
            "o = output.file(%wav, fallible=true, environment.get('OUT'), s)",
            "o.on_stop(synchronous=false, fun () -> shutdown())",
            "",
        ]
    )


def integrated_lufs(path: Path) -> float:
    stderr = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "ebur128",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    ).stderr
    # ebur128 logs a running I: per frame and then its summary; the last one is the summary.
    found = re.findall(r"I:\s+(-?[0-9.]+) LUFS", stderr)
    assert found, stderr
    return float(found[-1])


def test_agc_does_not_reapply_a_decks_item_gain(tmp_path: Path, liquidsoap_exe: Path) -> None:
    script = tmp_path / "agc.liq"
    script.write_text(render_script(up_to_agc(SESSION_SCRIPT.read_text("utf-8"))), "utf-8")
    out = tmp_path / "out.wav"
    env = {
        **os.environ,
        **cache_env(tmp_path / "cache"),
        "MASTER": loud_master(tmp_path / "master.flac").as_posix(),
        "ITEM_GAIN": str(ITEM_GAIN_DB),
        "OUT": out.as_posix(),
    }
    run = subprocess.run(
        [str(liquidsoap_exe), str(script)], env=env, capture_output=True, text=True, timeout=150
    )
    assert run.returncode == 0 and out.is_file(), run.stdout + run.stderr
    loudness = integrated_lufs(out)
    # The deck brings the master to -18 and the AGC rides it toward its -16 target (measured
    # -16.6). With the item gain applied a second time it comes out at -28 (measured -28.0).
    assert loudness == pytest.approx(-16.0, abs=1.5), (
        f"{loudness} LUFS: normalize re-applied liq_amplify "
        f"(double gain would give {MASTER_LUFS + 2 * ITEM_GAIN_DB})"
    )
