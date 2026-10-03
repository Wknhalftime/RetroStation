"""The ``SignOff`` value object (PR G1, Task 3; traceability F: T3.1-T3.3).

Requirements: PG3 (FLAC, MP3 or WAV; 1 s to 5 min; the stored file is content-named,
``<16 hex>.<format>``); house rule: value objects validate in ``__post_init__`` and name the
field (H4); error handling: a stored setting that is not a sign-off is refused, never
half-read.
"""

from __future__ import annotations

import json

import pytest

from backend.domain.streaming import ClipFormat, InvalidStreamValueError, SignOff

GOOD = {
    "file_name": "0123456789abcdef.flac",
    "format": ClipFormat.FLAC,
    "span_ms": 4_000,
    "name": "Station ID.flac",
}


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"span_ms": 999}, "span_ms"),
        ({"span_ms": 300_001}, "span_ms"),
        ({"file_name": "0123456789ABCDEF.flac"}, "file_name"),
        ({"file_name": "0123456789abcdef.mp3"}, "file_name"),
        ({"name": ""}, "name"),
        ({"name": "n" * 256}, "name"),
        ({"file_name": "../0123456789abcdef.flac"}, "file_name"),
        ({"file_name": "x/0123456789abcdef.flac"}, "file_name"),
    ],
    ids=[
        "span-999",
        "span-300001",
        "not-hex",
        "extension-not-format",
        "empty-name",
        "long-name",
        "parent-folder",
        "sub-folder",
    ],
)
def test_a_sign_off_refuses_bad_values_naming_the_field(
    change: dict[str, object], field: str
) -> None:
    # T3.1 (PG3, H4): the file name is exactly <16 hex>.<format>, so no path can escape the
    # sign-off folder (audit SF3).
    with pytest.raises(InvalidStreamValueError, match=rf"SignOff\.{field}"):
        SignOff(**{**GOOD, **change})  # type: ignore[arg-type]


def test_a_sign_off_round_trips_through_its_setting() -> None:
    # T3.2 (PG3): the setting holds JSON that reads back as the same value; the limits are
    # inclusive (1 s and 5 min; a 255-character name are allowed).
    cases = ((1_000, ClipFormat.MP3, "Ünïcode name.mp3"), (300_000, ClipFormat.WAV, "n" * 255))
    for span_ms, file_format, name in cases:
        clip = SignOff(
            file_name=f"fedcba9876543210.{file_format}",
            format=file_format,
            span_ms=span_ms,
            name=name,
        )
        assert SignOff.from_setting(clip.to_setting()) == clip


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps({"file_name": "0123456789abcdef.flac", "format": "flac", "name": "a"}),
        json.dumps(
            {"file_name": "../escape.flac", "format": "flac", "span_ms": 4_000, "name": "a"}
        ),
    ],
    ids=["not-json", "no-span", "bad-file-name"],
)
def test_a_setting_that_is_not_a_sign_off_is_refused(value: str) -> None:
    # T3.3 (error handling): refused as a streaming value error, never a raw decoding error.
    with pytest.raises(InvalidStreamValueError):
        SignOff.from_setting(value)
