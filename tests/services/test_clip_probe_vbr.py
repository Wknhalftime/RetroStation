"""A VBR MP3 with no length header probes at its true length (PR G1 fix; PG14, D26).

Requirements: PG14 (a sign-off clip plays whole: its probed length becomes the engine's cue
out); D26 (a sign-off lasts 1 s to 5 minutes, judged on its true length). mutagen estimates a
VBR MP3 that has no Xing, VBRI or Info header from its first frame's bitrate, which is wrong;
the ruling measures such a file by its frames and keeps mutagen's exact value whenever a
header is present.

The MP3s are made at test time with ffmpeg (pink noise, so the encoder's bitrate varies); the
module skips when ffmpeg is not on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import mutagen
import pytest

from backend.domain.streaming import ClipFormat, ClipLengthError
from backend.services.audio_tags import probe_clip
from backend.services.streaming.sign_off import ClipUpload, SignOffPorts, save_sign_off
from tests.fakes.user_settings import FakeUserSettingRepository

FFMPEG = shutil.which("ffmpeg")

pytestmark = pytest.mark.skipif(
    FFMPEG is None, reason="ffmpeg is not on PATH: these tests encode their MP3s with it"
)

SECONDS = 20
TOLERANCE_MS = 500
NO_LENGTH_HEADER = ["-write_xing", "0"]


def encode(out: Path, source: str, *options: str) -> Path:
    """``source`` (a lavfi graph) encoded to the MP3 ``out`` with ``options``."""
    assert FFMPEG is not None
    command = [
        FFMPEG,
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        source,
        *options,
        "-f",
        "mp3",
        str(out),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return out


def pink_noise(seconds: int) -> str:
    return f"anoisesrc=d={seconds}:c=pink:a=0.3"


def mutagen_span_ms(path: Path) -> int:
    audio = mutagen.File(str(path))  # type: ignore[attr-defined]
    assert audio is not None
    length: float = audio.info.length
    return round(length * 1000)


@pytest.mark.parametrize(
    ("rate", "channels"),
    [("48000", "2"), ("22050", "2"), ("11025", "1")],
    ids=["mpeg1-stereo", "mpeg2-stereo", "mpeg2.5-mono"],
)
def test_a_vbr_mp3_without_a_length_header_probes_at_its_true_length(
    tmp_path: Path, rate: str, channels: str
) -> None:
    path = encode(
        tmp_path / "clip.partial",
        pink_noise(SECONDS),
        *("-ar", rate, "-ac", channels, "-q:a", "2", *NO_LENGTH_HEADER),
    )
    probed = probe_clip(path)
    assert probed.format is ClipFormat.MP3
    assert abs(probed.span_ms - SECONDS * 1000) <= TOLERANCE_MS, probed


def test_tags_at_either_end_do_not_count_as_audio(tmp_path: Path) -> None:
    # An ID3v2 tag with a large comment leads the file and an ID3v1 tag ends it; an APE tag
    # is appended before the ID3v1 tag. None of them adds or removes a frame's worth of time.
    plain = encode(
        tmp_path / "plain.partial",
        pink_noise(SECONDS),
        *("-q:a", "2", *NO_LENGTH_HEADER, "-id3v2_version", "0"),
    )
    tagged = encode(
        tmp_path / "tagged.mp3",
        pink_noise(SECONDS),
        *("-q:a", "2", *NO_LENGTH_HEADER, "-write_id3v1", "1"),
        *("-metadata", "comment=" + "sign-off " * 500, "-metadata", "title=Sign-off"),
    )
    data = tagged.read_bytes()
    id3v1, audio = data[-128:], data[:-128]
    assert id3v1[:3] == b"TAG"
    staged = tmp_path / "staged.partial"
    staged.write_bytes(audio + ape_tag() + id3v1)
    assert abs(probe_clip(staged).span_ms - probe_clip(plain).span_ms) <= 1


def ape_tag() -> bytes:
    """An APEv2 tag (header, one item, footer) whose item value looks like MPEG frames."""
    value = b"\xff\xfb\x90\x00" * 64
    item = len(value).to_bytes(4, "little") + bytes(4) + b"Comment\x00" + value
    size = len(item) + 32  # the items plus the footer, never the header

    def block(flags: int) -> bytes:
        return (
            b"APETAGEX"
            + (2000).to_bytes(4, "little")
            + size.to_bytes(4, "little")
            + (1).to_bytes(4, "little")
            + flags.to_bytes(4, "little")
            + bytes(8)
        )

    has_header, is_header = 0x80000000, 0x20000000
    return block(has_header | is_header) + item + block(has_header)


@pytest.mark.parametrize(
    "options",
    [["-q:a", "2"], ["-b:a", "128k"]],
    ids=["vbr-with-xing", "cbr-with-info"],
)
def test_an_mp3_with_a_length_header_keeps_mutagens_length(
    tmp_path: Path, options: list[str]
) -> None:
    path = encode(tmp_path / "clip.partial", pink_noise(SECONDS), *options)
    probed = probe_clip(path)
    assert probed.span_ms == mutagen_span_ms(path)
    assert abs(probed.span_ms - SECONDS * 1000) <= TOLERANCE_MS


def test_the_five_minute_limit_applies_to_the_true_length(tmp_path: Path) -> None:
    # 3 s of noise, then silence to 5.5 minutes: the loud first frame makes mutagen's
    # estimate far shorter than the clip (about 42 s), which would pass the limit.
    path = encode(
        tmp_path / "long.mp3",
        f"{pink_noise(3)},apad=whole_dur=330",
        *("-ac", "1", "-q:a", "2", *NO_LENGTH_HEADER),
    )
    assert mutagen_span_ms(path) < 300_000
    assert abs(probe_clip(path).span_ms - 330_000) <= TOLERANCE_MS

    folder = tmp_path / "sign-off"
    folder.mkdir()
    ports = SignOffPorts(
        settings=FakeUserSettingRepository(),
        folder=folder,
        probe=probe_clip,
        commit=lambda: None,
    )
    with pytest.raises(ClipLengthError):
        save_sign_off(ports, ClipUpload(name="long.mp3", data=path.read_bytes()))
