"""Preset files: what a chain variant is, validated on load, with superseded values kept."""

from __future__ import annotations

from pathlib import Path

import pytest
from processor_harness.presets import Backend, PresetError, load_preset


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_liquidsoap_preset_resolves_its_chain_next_to_the_preset(tmp_path: Path) -> None:
    write(tmp_path / "chain.liq", "def chain(s) = s end\n")
    preset = load_preset(
        write(
            tmp_path / "p.toml",
            'id = "agc-only"\nversion = 2\nbackend = "liquidsoap"\nchain = "chain.liq"\n'
            'description = "AGC, nothing else"\n'
            '[params]\ntarget = -16.0\nhold = true\nwindow = 3\nmode = "lufs"\n',
        )
    )

    assert preset.id == "agc-only"
    assert preset.version == 2
    assert preset.backend is Backend.LIQUIDSOAP
    assert preset.chain == tmp_path / "chain.liq"
    assert dict(preset.params) == {"target": -16.0, "hold": True, "window": 3, "mode": "lufs"}
    assert preset.ceiling_dbtp == -1.0  # P5's starting point


def test_ffmpeg_preset_carries_its_graph(tmp_path: Path) -> None:
    preset = load_preset(
        write(
            tmp_path / "p.toml",
            'id = "x"\nversion = 1\nbackend = "ffmpeg"\ngraph = "[0:a]volume=$gain[out]"\n'
            "ceiling_dbtp = -1.5\n[params]\ngain = 0.5\n",
        )
    )

    assert preset.backend is Backend.FFMPEG
    assert preset.graph == "[0:a]volume=$gain[out]"
    assert preset.chain is None
    assert preset.ceiling_dbtp == -1.5


def test_superseded_values_are_kept_with_their_date_and_reason(tmp_path: Path) -> None:
    preset = load_preset(
        write(
            tmp_path / "p.toml",
            'id = "x"\nversion = 2\nbackend = "ffmpeg"\ngraph = "[0:a]anull[out]"\n'
            "[params]\ndrive = 11.0\n"
            '[[superseded]]\nparam = "drive"\nvalue = 13.0\ndate = "2026-10-24"\n'
            'reason = "band 3 over-compressed"\n',
        )
    )

    (old,) = preset.superseded
    assert (old.param, old.value, old.date, old.reason) == (
        "drive",
        13.0,
        "2026-10-24",
        "band 3 over-compressed",
    )


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ('version = 1\nbackend = "ffmpeg"\ngraph = "g"\n', "id"),
        ('id = "x"\nbackend = "ffmpeg"\ngraph = "g"\n', "version"),
        ('id = "x"\nversion = 1\nbackend = "sox"\n', "backend"),
        ('id = "x"\nversion = 1\nbackend = "ffmpeg"\n', "graph"),
        ('id = "x"\nversion = 1\nbackend = "liquidsoap"\nchain = "missing.liq"\n', "chain"),
        ('id = "x"\nversion = 1\nbackend = "ffmpeg"\ngraph = "g"\n[params]\nbad = [1]\n', "bad"),
        (
            'id = "x"\nversion = 1\nbackend = "ffmpeg"\ngraph = "g"\n[[superseded]]\nparam = "a"\n',
            "superseded",
        ),
    ],
)
def test_invalid_presets_name_the_file_and_the_field(tmp_path: Path, body: str, field: str) -> None:
    path = write(tmp_path / "bad.toml", body)

    with pytest.raises(PresetError, match=rf"bad\.toml.*{field}"):
        load_preset(path)
