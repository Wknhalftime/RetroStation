"""session.liq's AGC never re-applies an item's liq_amplify (real Liquidsoap 2.4.5, offline).

On 2.4.5 ``amplify``'s ``override`` defaults to ``liq_amplify``: in an AGC that ends in
``amplify`` (the stdlib ``normalize`` did), a track boundary carrying it replaces the AGC
gain with the item gain, so the gain lands twice and the AGC stops riding (measured -28.5
LUFS instead of -16 on Killing in the Name, before R17). In session.liq the decks' ``add()``
happens to hide those boundaries, and so does the ffmpeg graph of the R17 chain's DC blocker
ahead of its AGC (R17 on air, Lance 2026-10-27). This test feeds a deck straight into what
session.liq does after the mix, through the chain without its DC blocker, so the guards (the
metadata filter and agc.liq's ``override=null``) have to hold on their own.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from backend.playout.liquidsoap_process import SESSION_SCRIPT, cache_env

pytestmark = [pytest.mark.slow, pytest.mark.timeout(180)]

ITEM_GAIN_DB = -10.0  # the incident's item: a -7 LUFS master at -10.5 dB
MASTER_LUFS = -8.0  # measured: 500 Hz sine at the default 1/8 amplitude, raised 13.7 dB
SPAN_S = 20

CHAIN_PARAMS = SESSION_SCRIPT.with_name("chain_params.liq")

# From the line after the mix is built to the processed source: everything that sits between
# the decks and the stream's output in session.liq, the R17 chain included.
_AFTER_MIX = re.compile(r'^s = add\(id="main_and_bell".*\n', re.MULTILINE)
_PROCESSED = re.compile(r"^s = mksafe\(processed\)\n", re.MULTILINE)
# The chain's DC blocker, the graph that would hide the boundary (measured: with both guards
# gone the full chain still came out at -13.4; without this line, -21.2).
_HIGH_PASS = "  s = through_graph(s, high_pass_sections)\n"


def up_to_processed(script: str) -> str:
    start, end = _AFTER_MIX.search(script), _PROCESSED.search(script)
    assert start and end, "session.liq no longer has the anchors this test splices on"
    assert start.end() <= end.start(), "the processing moved before the mix"
    return script[start.end() : end.end()]


def chain_without_high_pass(work: Path) -> Path:
    """A copy of the on-air chain.liq, beside copies of the scripts it includes, minus its
    DC blocker."""
    work.mkdir()
    for script in SESSION_SCRIPT.parent.glob("*.liq"):
        shutil.copyfile(script, work / script.name)
    chain = (work / "chain.liq").read_text("utf-8")
    assert _HIGH_PASS in chain, "chain.liq no longer has the DC blocker this test takes out"
    (work / "chain.liq").write_text(chain.replace(_HIGH_PASS, ""), "utf-8")
    return work / "chain.liq"


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


def render_script(processing: str, chain: Path) -> str:
    """One deck (as mkdeck builds it) carrying liq_amplify, then session.liq's processing."""
    return "\n".join(
        [
            "settings.log.level := 2",
            f'%include "{CHAIN_PARAMS.as_posix()}"',
            f'%include "{chain.as_posix()}"',
            "song_started = ref(fun () -> ())",
            'm = once(single("annotate:liq_amplify=\\"#{environment.get(\'ITEM_GAIN\')} dB\\":'
            "#{environment.get('MASTER')}\"))",
            's = amplify(1., override="liq_amplify", m)',
            processing,
            # The chain's own output ends with the deck; mksafe would go on for ever.
            'clock.assign_new(sync="none", [processed])',
            "o = output.file(%wav, fallible=true, environment.get('OUT'), processed)",
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
    chain = chain_without_high_pass(tmp_path / "playout")
    processing = up_to_processed(SESSION_SCRIPT.read_text("utf-8"))
    script = tmp_path / "spliced.liq"
    script.write_text(render_script(processing, chain), "utf-8")
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
    # The deck brings the master to -18, where the R17 AGC idles (its -18 target), and the
    # chain's makeup lifts it to about R8's -14 (measured -13.4). With the item gain applied a
    # second time the AGC is overridden at -28 and the chain puts out -21 (measured -21.2).
    assert loudness == pytest.approx(-14.0, abs=1.5), (
        f"{loudness} LUFS: the AGC re-applied liq_amplify "
        f"(double gain hands it {MASTER_LUFS + 2 * ITEM_GAIN_DB})"
    )
