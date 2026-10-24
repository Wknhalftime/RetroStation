"""Blind listening tests: level-matched clips, shuffled trials, every pick recorded, scored with
math item 13's binomial p-value."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
from processor_harness import audio_io, codec, listening, signals
from processor_harness.listening import Clip, Mode
from processor_harness.metrics import loudness

FS = 44100
FFMPEG = shutil.which("ffmpeg")


def clips(n: int) -> list[Clip]:
    return [
        Clip(f"song-{i}", {"liq": Path(f"liq-{i}.wav"), "norm": Path(f"norm-{i}.wav")})
        for i in range(n)
    ]


class FakePlayer:
    def __init__(self) -> None:
        self.played: list[Path] = []
        self.stopped = 0

    def play(self, path: Path) -> None:
        self.played.append(path)

    def stop(self) -> None:
        self.stopped += 1


def answers(*lines: str) -> Callable[[str], str]:
    """What the listener types, one line per prompt."""
    queue = list(lines)
    return lambda prompt: queue.pop(0)


def test_binomial_p_matches_math_item_13() -> None:
    assert listening.binomial_p(12, 16) == pytest.approx(0.0384, abs=1e-4)
    assert listening.binomial_p(13, 16) == pytest.approx(0.0106, abs=1e-4)
    assert listening.binomial_p(14, 16) == pytest.approx(0.0021, abs=1e-4)
    assert listening.binomial_p(0, 16) == 1.0


def test_a_session_has_16_trials_that_use_every_clip_and_shuffle_the_labels() -> None:
    session = listening.make_session(clips(5), ("liq", "norm"), Mode.ABX, trials=16, seed=7)

    assert len(session.trials) == 16
    assert {t.clip for t in session.trials} == {f"song-{i}" for i in range(5)}
    assert {t.labels["A"] for t in session.trials} == {"liq", "norm"}
    for t in session.trials:
        assert {t.labels["A"], t.labels["B"]} == {"liq", "norm"}
        assert t.labels["X"] in (t.labels["A"], t.labels["B"])


def test_sessions_are_reproducible_from_their_seed() -> None:
    a = listening.make_session(clips(3), ("liq", "norm"), Mode.PREFERENCE, trials=16, seed=3)
    b = listening.make_session(clips(3), ("liq", "norm"), Mode.PREFERENCE, trials=16, seed=3)

    assert a.trials == b.trials
    assert all("X" not in t.labels for t in a.trials)


def test_running_plays_by_label_and_records_every_pick(tmp_path: Path) -> None:
    session = listening.make_session(clips(2), ("liq", "norm"), Mode.ABX, trials=2, seed=1)
    session = listening.save_session(session, tmp_path)
    player = FakePlayer()
    first = session.trials[0]

    listening.run_session(session, player, answers("a", "x", "1", "b", "2"))

    assert player.played[0] == session.clip_path(first.clip, first.labels["A"])
    assert player.played[1] == session.clip_path(first.clip, first.labels["X"])
    picks = [json.loads(line) for line in (tmp_path / "abx-picks.jsonl").read_text().splitlines()]
    assert [p["trial"] for p in picks] == [1, 2]
    assert [p["choice"] for p in picks] == ["A", "B"]


def test_a_stopped_session_resumes_at_the_first_unanswered_trial(tmp_path: Path) -> None:
    session = listening.save_session(
        listening.make_session(clips(2), ("liq", "norm"), Mode.PREFERENCE, trials=3, seed=2),
        tmp_path,
    )
    listening.run_session(session, FakePlayer(), answers("1", "q"))

    listening.run_session(session, FakePlayer(), answers("2", "1"))

    picks = [
        json.loads(line) for line in (tmp_path / "preference-picks.jsonl").read_text().splitlines()
    ]
    assert [p["trial"] for p in picks] == [1, 2, 3]


def test_abx_score_counts_correct_identifications() -> None:
    session = listening.make_session(clips(4), ("liq", "norm"), Mode.ABX, trials=16, seed=5)
    picks = [
        {"trial": t.number, "choice": "A" if t.labels["X"] == t.labels["A"] else "B"}
        for t in session.trials
    ]
    picks[0]["choice"] = "B" if picks[0]["choice"] == "A" else "A"  # one miss

    result = listening.score(session, picks)

    assert (result.correct, result.trials) == (15, 16)
    assert result.p_value == pytest.approx(listening.binomial_p(15, 16))


def test_preference_score_counts_wins_for_each_preset() -> None:
    session = listening.make_session(clips(4), ("liq", "norm"), Mode.PREFERENCE, trials=16, seed=5)
    picks = [
        {"trial": t.number, "choice": "A" if t.labels["A"] == "norm" else "B"}
        for t in session.trials
    ]

    result = listening.score(session, picks)

    assert result.wins == {"liq": 0, "norm": 16}
    assert result.p_value == pytest.approx(2 * listening.binomial_p(16, 16))


@pytest.mark.slow
@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not on PATH")
def test_clips_are_cut_from_the_mp3s_and_level_matched(tmp_path: Path) -> None:
    # Two renders of one song 6 dB apart: after matching, both clips read the same LUFS,
    # neither clips, and each lasts the requested 4 s.
    assert FFMPEG is not None
    song = signals.noise_with_pauses(FS, burst_s=10.0, pause_s=0.1, bursts=1, level_dbfs=-20.0)
    mp3s = {}
    for name, gain_db in (("liq", 0.0), ("norm", -6.0)):
        wav = tmp_path / f"{name}.wav"
        audio_io.write_wav(wav, song.samples * 10 ** (gain_db / 20), FS)
        mp3s[name] = codec.encode_mp3(Path(FFMPEG), wav, tmp_path / f"{name}.mp3")

    clip = listening.prepare_clip(
        "song", mp3s, start_s=2.0, duration_s=4.0, directory=tmp_path / "clips", ffmpeg=Path(FFMPEG)
    )

    levels = []
    for path in clip.paths.values():
        x, fs = audio_io.read_wav(path)
        assert fs == FS and len(x) == 4 * FS
        assert np.max(np.abs(x)) < 10 ** (-0.5 / 20)
        levels.append(loudness.integrated(x, fs))
    assert levels[0] == pytest.approx(levels[1], abs=0.1)
