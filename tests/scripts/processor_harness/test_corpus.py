"""The corpus manifest: synthetic stimuli by kind, programme excerpts by path."""

from __future__ import annotations

from pathlib import Path

import pytest
from processor_harness.corpus import CorpusError, load_corpus


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_signals_build_at_the_corpus_rate(tmp_path: Path) -> None:
    corpus = load_corpus(
        write(
            tmp_path / "c.toml",
            "sample_rate = 44100\n"
            '[[signal]]\nid = "imp"\nkind = "impulse"\nlevel_dbfs = -30.0\nseconds = 1.0\n'
            "at_s = 0.25\n",
        )
    )

    (entry,) = corpus.signals
    stimulus = entry.build(corpus.sample_rate)
    assert entry.id == "imp" and entry.kind == "impulse"
    assert stimulus.fs == 44100
    assert stimulus.samples.shape == (44100, 2)


def test_programme_entries_resolve_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "song.flac").write_bytes(b"")
    corpus = load_corpus(
        write(
            tmp_path / "c.toml",
            'sample_rate = 44100\n[[programme]]\nid = "song"\npath = "song.flac"\n'
            'category = "music"\nstart_s = 30.0\nduration_s = 60.0\n',
        )
    )

    (song,) = corpus.programme
    assert song.path == tmp_path / "song.flac"
    assert (song.category, song.start_s, song.duration_s) == ("music", 30.0, 60.0)


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ('[[signal]]\nid = "a"\nkind = "impulse"\n', "sample_rate"),
        ('sample_rate = 44100\n[[signal]]\nid = "a"\nkind = "chirp"\n', "kind"),
        ('sample_rate = 44100\n[[signal]]\nid = "a"\nkind = "impulse"\nwobble = 1.0\n', "a"),
        (
            'sample_rate = 44100\n[[programme]]\nid = "s"\npath = "nope.flac"\n'
            'category = "music"\nstart_s = 0.0\nduration_s = 1.0\n',
            "path",
        ),
        (
            'sample_rate = 44100\n[[programme]]\nid = "s"\npath = "c.toml"\n'
            'category = "jingle"\nstart_s = 0.0\nduration_s = 1.0\n',
            "category",
        ),
        (
            'sample_rate = 44100\n[[signal]]\nid = "a"\nkind = "impulse"\nlevel_dbfs = -30.0\n'
            'seconds = 1.0\nat_s = 0.0\n[[signal]]\nid = "a"\nkind = "impulse"\n'
            "level_dbfs = -30.0\nseconds = 1.0\nat_s = 0.0\n",
            "id",
        ),
    ],
)
def test_invalid_manifests_name_the_file_and_the_field(
    tmp_path: Path, body: str, field: str
) -> None:
    path = write(tmp_path / "c.toml", body)

    with pytest.raises(CorpusError, match=rf"c\.toml.*{field}"):
        load_corpus(path)
