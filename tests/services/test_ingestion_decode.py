"""Unit tests for the CSV decode ladder in ingestion_service.

The ladder must:
- Accept UTF-8 with and without BOM.
- Reject chardet's "ascii" guess when the file actually contains multi-byte
  UTF-8 bytes (the original production bug).
- Accept a confident non-ascii encoding detected by chardet.
- Raise CsvDecodeError (not a raw UnicodeDecodeError) on anything it cannot
  confidently decode, instead of silently mojibake-ing the content.
"""
from __future__ import annotations

import pytest

from backend.services.ingestion_service import (
    CsvDecodeError,
    _decode_csv_bytes,
)

_BOM = b"\xef\xbb\xbf"


def test_plain_ascii_decodes_as_utf8() -> None:
    assert _decode_csv_bytes(b"Artist,Title\nFoo,Bar\n") == "Artist,Title\nFoo,Bar\n"


def test_utf8_with_multibyte_characters_decodes() -> None:
    # The production crash was on byte 0xe2 (first byte of an em-dash in UTF-8).
    payload = "Artist,Title\nBeyoncé,Déjà Vu — live\n".encode()
    assert _decode_csv_bytes(payload) == payload.decode()


def test_utf8_bom_is_stripped() -> None:
    body = "Artist,Title\nFoo,Bar\n"
    assert _decode_csv_bytes(_BOM + body.encode("utf-8")) == body


def test_utf16_le_with_bom_is_accepted_via_chardet() -> None:
    body = "Artist,Title\nFoo,Bar\n"
    # Encoding with a BOM gives chardet a high-confidence signal.
    payload = body.encode("utf-16")
    assert _decode_csv_bytes(payload) == body


def test_raises_csv_decode_error_on_garbage() -> None:
    # Random high bytes without a BOM: not valid UTF-8, and chardet either
    # fails or returns a low-confidence / ascii guess we reject.
    payload = bytes(range(128, 256)) * 20
    with pytest.raises(CsvDecodeError):
        _decode_csv_bytes(payload)


def test_bom_with_invalid_trailing_bytes_falls_through_to_error() -> None:
    # Would have previously leaked a raw UnicodeDecodeError when the BOM
    # branch short-circuited without try/except.
    payload = _BOM + b"\xff\xfe\xfd garbage"
    with pytest.raises(CsvDecodeError):
        _decode_csv_bytes(payload)
