"""Acceptance tests: why a file is on the Library Silent Files report (S2, S6; issue #146).

- silent: its audio's cue row is marked silent (D111), whatever the file's length.
- low_bitrate: under 64 kb/s on disk (file_size * 8 / duration_ms), for files over 30 s.
- short_cued_span: a real (not fallback) cue row whose span is under 10 % of the file, for
  files over 30 s.
Integer arithmetic, so the database pre-filter and the domain agree at every boundary. Measured
read-only on dev, 2026-10-16: each nearly-silent signal flags exactly Testimony (17 kb/s, 3.9 %);
the next file is at 182 kb/s and 22.7 %. Read as attributes, so each test fails alone.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.domain import streaming


def entry(**overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "file_id": uuid4(),
        "path": "D:/m/a.flac",
        "artist": "Artist",
        "title": "Title",
        "album": "Album",
        "duration_ms": 200_000,
        "file_size": 30_000_000,
        "audio_silent": False,
        "cued_span_ms": 180_000,
    }
    return streaming.SilentFileEntry(**{**fields, **overrides})


def test_a_silent_row_is_listed_whatever_its_length() -> None:
    silent = entry(audio_silent=True, duration_ms=6_000, file_size=1_000, cued_span_ms=None)
    assert silent.reasons() == (streaming.SilenceReason.SILENT,)


@pytest.mark.parametrize(
    ("file_size", "listed"), [(1_599_999, True), (1_600_000, False)], ids=["under", "at 64"]
)
def test_the_low_bitrate_signature(file_size: int, listed: bool) -> None:
    """200 s at 1,600,000 bytes is exactly 64 kb/s: not under it."""
    expected = (streaming.SilenceReason.LOW_BITRATE,) if listed else ()
    assert entry(file_size=file_size).reasons() == expected


@pytest.mark.parametrize(
    ("cued_span_ms", "listed"), [(19_999, True), (20_000, False)], ids=["under", "at 10 %"]
)
def test_the_short_cued_span(cued_span_ms: int, listed: bool) -> None:
    expected = (streaming.SilenceReason.SHORT_CUED_SPAN,) if listed else ()
    assert entry(cued_span_ms=cued_span_ms).reasons() == expected


@pytest.mark.parametrize(
    ("duration_ms", "listed"), [(30_000, False), (30_001, True)], ids=["30 s", "over 30 s"]
)
def test_the_nearly_silent_signals_need_a_file_over_30_seconds(
    duration_ms: int, listed: bool
) -> None:
    both = (streaming.SilenceReason.LOW_BITRATE, streaming.SilenceReason.SHORT_CUED_SPAN)
    tiny = entry(duration_ms=duration_ms, file_size=1_000, cued_span_ms=1_000)
    assert tiny.reasons() == (both if listed else ())


def test_without_a_size_or_a_real_cue_row_there_is_no_nearly_silent_signal() -> None:
    """cued_span_ms is None without a cue row, and for a fallback (failed) row."""
    unknown = entry(file_size=None, cued_span_ms=None)
    assert (unknown.reasons(), unknown.bitrate_kbps()) == ((), None)
    assert entry(duration_ms=None, cued_span_ms=None).bitrate_kbps() is None


def test_testimony_is_listed_for_both_signals() -> None:
    testimony = entry(duration_ms=298_666, file_size=632_875, cued_span_ms=11_700)
    assert testimony.reasons() == (
        streaming.SilenceReason.LOW_BITRATE,
        streaming.SilenceReason.SHORT_CUED_SPAN,
    )
    assert testimony.bitrate_kbps() == 17


def test_the_report_counts_silent_and_nearly_silent() -> None:
    """A long silent file is also low-bitrate: one entry, both reasons, counted as silent."""
    silent_and_small = entry(audio_silent=True, file_size=1_000, cued_span_ms=None)
    assert silent_and_small.reasons() == (
        streaming.SilenceReason.SILENT,
        streaming.SilenceReason.LOW_BITRATE,
    )
    report = streaming.SilentFileReport((silent_and_small, entry(file_size=1_000)))
    assert (report.silent, report.nearly_silent) == (1, 1)
