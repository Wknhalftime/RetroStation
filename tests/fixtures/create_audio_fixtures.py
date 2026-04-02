"""
Generate synthetic audio fixture files for library scan tests.

Produces 5 files in tests/fixtures/audio/:
  well_tagged.mp3   — Tier 1: all MusicBrainz IDs (ID3 TXXX frames)
  partial_tags.mp3  — Tier 2: title/artist but no MBIDs
  minimal_tags.ogg  — Tier 3: basic Vorbis comment tags
  no_tags.wav       — Tier 3: untagged WAV
  corrupt.mp3       — garbage bytes (triggers quarantine)
"""

from __future__ import annotations

import struct
import subprocess
import sys
import wave
from pathlib import Path

from mutagen.id3 import (  # type: ignore[attr-defined]
    ID3,
    TALB,
    TIT2,
    TPE1,
    TPOS,
    TRCK,
    TXXX,
    Encoding,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
FIXTURES_DIR = Path(__file__).parent / "audio"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# MP3 helpers
# ---------------------------------------------------------------------------

def _make_silent_mp3_frames(num_frames: int = 8) -> bytes:
    """
    Return raw MPEG1 Layer3 silence frames.

    Frame header: 0xFF 0xFB (MPEG1, Layer3, 128kbps, 44100Hz, joint stereo)
    Each frame at 128kbps/44100Hz is 417 bytes total.
    """
    header = bytes([0xFF, 0xFB, 0x90, 0x00])
    payload_size = 417 - 4
    frame = header + b"\x00" * payload_size
    return frame * num_frames


def _write_id3v2_plain(tags: dict[str, str]) -> bytes:
    """Produce a minimal ID3v2.3 blob for standard text frames only (latin-1)."""

    def _text_frame(frame_id: str, text: str) -> bytes:
        content = b"\x00" + text.encode("latin-1", errors="replace")
        return (
            frame_id.encode("ascii")
            + struct.pack(">I", len(content))
            + b"\x00\x00"
            + content
        )

    body = b"".join(_text_frame(fid, txt) for fid, txt in tags.items())
    size = len(body)
    syncsafe = (
        ((size >> 21) & 0x7F) << 24
        | ((size >> 14) & 0x7F) << 16
        | ((size >> 7) & 0x7F) << 8
        | (size & 0x7F)
    )
    return b"ID3\x03\x00\x00" + struct.pack(">I", syncsafe) + body


# ---------------------------------------------------------------------------
# Fixture 1: well_tagged.mp3  (use mutagen API for correct TXXX encoding)
# ---------------------------------------------------------------------------

def create_well_tagged_mp3(path: Path) -> None:
    # Write bare MP3 frames first, then layer ID3 tags on top with mutagen
    mp3_frames = _make_silent_mp3_frames(8)
    path.write_bytes(mp3_frames)

    tags = ID3()
    tags.add(TIT2(encoding=Encoding.UTF8, text=["Test Track One"]))
    tags.add(TPE1(encoding=Encoding.UTF8, text=["Test Artist"]))
    tags.add(TALB(encoding=Encoding.UTF8, text=["Test Album"]))
    tags.add(TRCK(encoding=Encoding.UTF8, text=["3/10"]))
    tags.add(TPOS(encoding=Encoding.UTF8, text=["1/2"]))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="MusicBrainz Track Id",
                  text=["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="MusicBrainz Artist Id",
                  text=["11111111-2222-3333-4444-555555555555"]))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="MusicBrainz Album Artist Id",
                  text=["66666666-7777-8888-9999-aaaaaaaaaaaa"]))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="MusicBrainz Album Id",
                  text=["bbbbbbbb-cccc-dddd-eeee-ffffffffffff"]))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="MusicBrainz Release Type",
                  text=["album"]))
    tags.add(TXXX(encoding=Encoding.UTF8, desc="MusicBrainz Release Status",
                  text=["official"]))
    tags.save(str(path), v2_version=3)
    print(f"  created {path.name}")


# ---------------------------------------------------------------------------
# Fixture 2: partial_tags.mp3
# ---------------------------------------------------------------------------

def create_partial_tags_mp3(path: Path) -> None:
    tags = {
        "TIT2": "Partial Track",
        "TPE1": "Some Artist",
        "TALB": "Some Album",
        "TRCK": "5",
    }
    id3 = _write_id3v2_plain(tags)
    mp3_frames = _make_silent_mp3_frames(8)
    path.write_bytes(id3 + mp3_frames)
    print(f"  created {path.name}")


# ---------------------------------------------------------------------------
# Fixture 3: minimal_tags.ogg  (via ffmpeg)
# ---------------------------------------------------------------------------

def create_minimal_ogg(path: Path) -> None:
    # Generate a silent WAV first, then convert to OGG via ffmpeg
    wav_tmp = path.with_suffix(".tmp.wav")
    _write_wav(wav_tmp)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", str(wav_tmp),
                "-c:a", "libvorbis",
                "-metadata", "title=Minimal Track",
                "-metadata", "artist=Minimal Artist",
                str(path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode())
        print(f"  created {path.name} (via ffmpeg)")
    finally:
        wav_tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# WAV helper
# ---------------------------------------------------------------------------

def _write_wav(path: Path, num_frames: int = 4410) -> None:
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(44100)
        wf.writeframes(b"\x00" * num_frames * 2 * 2)


# ---------------------------------------------------------------------------
# Fixture 4: no_tags.wav
# ---------------------------------------------------------------------------

def create_no_tags_wav(path: Path) -> None:
    _write_wav(path, num_frames=4410)
    print(f"  created {path.name}")


# ---------------------------------------------------------------------------
# Fixture 5: corrupt.mp3
# ---------------------------------------------------------------------------

def create_corrupt_mp3(path: Path) -> None:
    path.write_bytes(b"\x00\xFF\xFE\xAB\xCD" * 20 + b"not an mp3 at all!!!")
    print(f"  created {path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"Writing fixtures to: {FIXTURES_DIR}")
    create_well_tagged_mp3(FIXTURES_DIR / "well_tagged.mp3")
    create_partial_tags_mp3(FIXTURES_DIR / "partial_tags.mp3")
    try:
        create_minimal_ogg(FIXTURES_DIR / "minimal_tags.ogg")
    except Exception as exc:
        print(f"  WARNING: could not create minimal_tags.ogg ({exc}); tests will skip it")
    create_no_tags_wav(FIXTURES_DIR / "no_tags.wav")
    create_corrupt_mp3(FIXTURES_DIR / "corrupt.mp3")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
