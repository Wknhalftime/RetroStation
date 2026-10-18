"""The clip probe names what the clip is for in its unsupported-format message (final review
M1): the sign-off keeps "a sign-off must be ...", and the liners' real wiring
(``get_liner_ports``) says "a liner must be ...", so the Liners tab never mentions sign-offs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import mutagen
import pytest
from mutagen.oggvorbis import OggVorbis

from backend.dependencies import get_liner_ports
from backend.domain.streaming import UnsupportedClipError
from backend.services.audio_tags import probe_clip


@pytest.fixture
def ogg_clip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A staged clip that mutagen detects as Ogg Vorbis (unsupported)."""
    info = SimpleNamespace(length=3.0, sketchy=False)
    detected_kind = type("DetectedOggVorbis", (OggVorbis,), {"info": info})
    detected = detected_kind.__new__(detected_kind)
    monkeypatch.setattr(mutagen, "File", lambda *args, **kwargs: detected)
    path = tmp_path / "3f2b9c1e-0000-4000-8000-000000000000.partial"
    path.write_bytes(b"\x00" * 64)
    return path


def test_the_sign_off_probe_keeps_its_wording(ogg_clip: Path) -> None:
    with pytest.raises(UnsupportedClipError, match="; a sign-off must be FLAC, MP3 or WAV$"):
        probe_clip(ogg_clip)


def test_the_liner_wiring_names_a_liner(ogg_clip: Path) -> None:
    repos = SimpleNamespace(broadcast=SimpleNamespace(liners=object(), stations=object()))
    ports = get_liner_ports(repos)  # type: ignore[arg-type]
    with pytest.raises(UnsupportedClipError) as refused:
        ports.probe(ogg_clip)
    assert str(refused.value).endswith("; a liner must be FLAC, MP3 or WAV")
    assert "sign-off" not in str(refused.value)
