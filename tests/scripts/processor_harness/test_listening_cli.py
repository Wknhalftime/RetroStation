"""The listening-test command line: prepare sessions from a harness run, score the picks."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from processor_harness import audio_io, codec, listening, listening_cli, signals
from processor_harness.listening_cli import ClipSpecError, load_clip_specs

FS = 44100
FFMPEG = shutil.which("ffmpeg")


def test_clip_specs_carry_their_input_start_and_note(tmp_path: Path) -> None:
    path = tmp_path / "clips.toml"
    path.write_text(
        'duration_s = 15.0\n[[clip]]\ninput = "teen-spirit"\nstart_s = 0.0\nnote = "intro"\n',
        encoding="utf-8",
    )

    duration, (spec,) = load_clip_specs(path)

    assert duration == 15.0
    assert (spec.input, spec.start_s, spec.note) == ("teen-spirit", 0.0, "intro")


@pytest.mark.parametrize(
    "body",
    [
        '[[clip]]\ninput = "a"\nstart_s = 0.0\n',
        'duration_s = 25.0\n[[clip]]\ninput = "a"\nstart_s = 0.0\n',
        "duration_s = 15.0\n",
    ],
)
def test_bad_clip_specs_name_the_file(tmp_path: Path, body: str) -> None:
    path = tmp_path / "clips.toml"
    path.write_text(body, encoding="utf-8")

    with pytest.raises(ClipSpecError, match=r"clips\.toml"):
        load_clip_specs(path)


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_prepare_then_score_a_run(tmp_path: Path) -> None:
    assert FFMPEG is not None
    run = tmp_path / "run"
    presets = {}
    for preset_id, gain_db in (("liq", 0.0), ("norm", -4.0)):
        (run / f"{preset_id}-v1").mkdir(parents=True)
        song = signals.noise_with_pauses(FS, burst_s=12.0, pause_s=0.1, bursts=1, level_dbfs=-20.0)
        wav = tmp_path / f"{preset_id}.wav"
        audio_io.write_wav(wav, song.samples * 10 ** (gain_db / 20), FS)
        codec.encode_mp3(Path(FFMPEG), wav, run / f"{preset_id}-v1" / "song.mp3")
        preset = tmp_path / f"{preset_id}.toml"
        preset.write_text(
            f'id = "{preset_id}"\nversion = 1\nbackend = "ffmpeg"\ngraph = "[0:a]anull[out]"\n',
            encoding="utf-8",
        )
        presets[preset_id] = preset
    clips = tmp_path / "clips.toml"
    clips.write_text(
        'duration_s = 10.0\n[[clip]]\ninput = "song"\nstart_s = 1.0\nnote = "x"\n', encoding="utf-8"
    )

    code = listening_cli.main(
        ["prepare", "--run", str(run), "--clips", str(clips), "--a", str(presets["liq"]),
         "--b", str(presets["norm"]), "--name", "agc", "--ffmpeg", FFMPEG]
    )  # fmt: skip

    assert code == 0
    folder = run / "listening" / "agc"
    abx = listening.load_session(folder / "abx-session.json")
    assert len(abx.trials) == 16
    assert len(listening.load_session(folder / "preference-session.json").trials) == 16
    with abx.picks_path.open("w", encoding="utf-8") as f:
        for t in abx.trials:
            choice = "A" if t.labels["X"] == t.labels["A"] else "B"
            f.write(json.dumps({"trial": t.number, "clip": t.clip, "choice": choice}) + "\n")

    assert listening_cli.main(["score", str(folder)]) == 0

    report = (folder / "results.md").read_text(encoding="utf-8")
    assert "16/16" in report
    assert json.loads((folder / "abx-results.json").read_text())["correct"] == 16


def test_two_clips_from_one_input_get_distinct_ids(tmp_path: Path) -> None:
    path = tmp_path / "clips.toml"
    path.write_text(
        'duration_s = 15.0\n[[clip]]\ninput = "air"\nstart_s = 171.0\n'
        '[[clip]]\ninput = "air"\nstart_s = 314.0\n'
        '[[clip]]\nid = "intro"\ninput = "b"\nstart_s = 0.0\n',
        encoding="utf-8",
    )

    _, specs = load_clip_specs(path)

    assert [s.id for s in specs] == ["air-171s", "air-314s", "intro"]


def test_clip_ids_must_be_unique(tmp_path: Path) -> None:
    path = tmp_path / "clips.toml"
    path.write_text(
        'duration_s = 15.0\n[[clip]]\nid = "x"\ninput = "a"\nstart_s = 0.0\n'
        '[[clip]]\nid = "x"\ninput = "b"\nstart_s = 0.0\n',
        encoding="utf-8",
    )

    with pytest.raises(ClipSpecError, match=r"clips\.toml.*unique"):
        load_clip_specs(path)
