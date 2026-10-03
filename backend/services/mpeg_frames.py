"""The true duration of an MPEG audio stream that carries no length header, from its frames.

Public API:
  headerless_duration(data) -> float | None  (seconds, summed frame by frame; None when the
                                              first frame carries a Xing, Info or VBRI
                                              header, or no frames are found)

Why: for a VBR MP3 with no Xing, VBRI or Info header, a tag reader can only estimate the
length from the first frame's bitrate, and the estimate can be wrong by minutes. With a
header the reader's value is exact, so this module defers to it (returns None).

Reads MPEG 1, 2 and 2.5, Layers I, II and III, from bytes only (no mutagen, no I/O). Any
ID3v2 tags at the start and an ID3v1 or APEv2 tag at the end are skipped. After the first
frame, a header that does not parse, or does not match the first frame, is resynced over a
bounded window; the scan stops there when no frame pair is found. A final frame cut short is
not counted.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bitrates in kbit/s for bitrate indexes 1-14, keyed by (version bits, layer bits).
# Version bits: 0b11 MPEG 1, 0b10 MPEG 2, 0b00 MPEG 2.5. Layer bits: 0b11 I, 0b10 II, 0b01 III.
_V1_L1 = (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448)
_V1_L2 = (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384)
_V1_L3 = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
_V2_L1 = (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256)
_V2_L23 = (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)

_MPEG1, _MPEG2, _MPEG25 = 0b11, 0b10, 0b00
_LAYER1, _LAYER2, _LAYER3 = 0b11, 0b10, 0b01

_BITRATES_KBPS: dict[tuple[int, int], tuple[int, ...]] = {
    (_MPEG1, _LAYER1): _V1_L1,
    (_MPEG1, _LAYER2): _V1_L2,
    (_MPEG1, _LAYER3): _V1_L3,
    (_MPEG2, _LAYER1): _V2_L1,
    (_MPEG2, _LAYER2): _V2_L23,
    (_MPEG2, _LAYER3): _V2_L23,
    (_MPEG25, _LAYER1): _V2_L1,
    (_MPEG25, _LAYER2): _V2_L23,
    (_MPEG25, _LAYER3): _V2_L23,
}

_SAMPLE_RATES: dict[int, tuple[int, int, int]] = {
    _MPEG1: (44100, 48000, 32000),
    _MPEG2: (22050, 24000, 16000),
    _MPEG25: (11025, 12000, 8000),
}

_MONO = 0b11
_HEADER_BYTES = 4
_FIRST_SYNC_WINDOW = 1024 * 1024  # as far as mutagen looks for the first frame
_RESYNC_WINDOW = 8 * 1024  # a few of the largest frames (2 881 bytes at MPEG 1 L3 320k 32 kHz)

_ID3V2_HEADER_BYTES = 10
_ID3V2_FOOTER_FLAG = 0x10
_ID3V1_BYTES = 128
_APE_FOOTER_BYTES = 32
_APE_HAS_HEADER = 0x80000000

_XING_FRAMES_FLAG = 0x1
_VBRI_OFFSET = 36


@dataclass(frozen=True)
class _Frame:
    """One parsed MPEG audio frame header."""

    version: int
    layer: int
    rate_index: int
    mode: int
    sample_rate: int
    samples: int
    length: int

    def continues(self, first: _Frame) -> bool:
        """Whether this frame belongs to the same stream as ``first``."""
        return (self.version, self.layer, self.rate_index) == (
            first.version,
            first.layer,
            first.rate_index,
        )


def _samples_per_frame(version: int, layer: int) -> int:
    if layer == _LAYER1:
        return 384
    if layer == _LAYER3 and version != _MPEG1:
        return 576
    return 1152


def _parse_frame(data: bytes, pos: int) -> _Frame | None:
    """The frame whose header starts at ``pos``, or None when no valid header is there."""
    if pos + _HEADER_BYTES > len(data):
        return None
    header = int.from_bytes(data[pos : pos + _HEADER_BYTES], "big")
    version = (header >> 19) & 0b11
    layer = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0b1111
    rate_index = (header >> 10) & 0b11
    if header >> 21 != 0x7FF or version == 0b01 or layer == 0b00:
        return None
    if bitrate_index in (0, 0b1111) or rate_index == 0b11:
        return None  # free format, a bad bitrate or a reserved sample rate
    padding = (header >> 9) & 1
    bitrate = _BITRATES_KBPS[(version, layer)][bitrate_index - 1] * 1000
    sample_rate = _SAMPLE_RATES[version][rate_index]
    samples = _samples_per_frame(version, layer)
    if layer == _LAYER1:
        length = (12 * bitrate // sample_rate + padding) * 4
    else:
        length = samples // 8 * bitrate // sample_rate + padding
    mode = (header >> 6) & 0b11
    return _Frame(version, layer, rate_index, mode, sample_rate, samples, length)


def _audio_bounds(data: bytes) -> tuple[int, int]:
    """The ``[start, end)`` of the audio, past any ID3v2 tags and before ID3v1 and APE tags."""
    start = 0
    while data[start : start + 3] == b"ID3" and start + _ID3V2_HEADER_BYTES <= len(data):
        size_bytes = data[start + 6 : start + _ID3V2_HEADER_BYTES]
        size = 0
        for byte in size_bytes:
            size = (size << 7) | (byte & 0x7F)
        footer = _ID3V2_HEADER_BYTES if data[start + 5] & _ID3V2_FOOTER_FLAG else 0
        start += _ID3V2_HEADER_BYTES + size + footer
    end = len(data)
    if end - _ID3V1_BYTES >= start and data[end - _ID3V1_BYTES : end - 125] == b"TAG":
        end -= _ID3V1_BYTES
    footer_at = end - _APE_FOOTER_BYTES
    if footer_at >= start and data[footer_at : footer_at + 8] == b"APETAGEX":
        size = int.from_bytes(data[footer_at + 12 : footer_at + 16], "little")
        flags = int.from_bytes(data[footer_at + 20 : footer_at + 24], "little")
        header = _APE_FOOTER_BYTES if flags & _APE_HAS_HEADER else 0
        end = max(start, end - size - header)
    return start, min(end, len(data))


def _frame_pair_at(data: bytes, pos: int, end: int, first: _Frame | None) -> _Frame | None:
    """The frame at ``pos`` when it, and the frame after it, are valid (and continue ``first``,
    if given); a frame that ends exactly at ``end`` needs no successor."""
    frame = _parse_frame(data, pos)
    if frame is None or (first is not None and not frame.continues(first)):
        return None
    after = pos + frame.length
    if after == end:
        return frame
    successor = _parse_frame(data, after) if after < end else None
    if successor is None or not successor.continues(frame):
        return None
    return frame


def _sync(data: bytes, pos: int, end: int, window: int, first: _Frame | None) -> int | None:
    """The offset of the first frame pair in ``[pos, pos + window)``, or None."""
    stop = min(end, pos + window)
    candidate = data.find(b"\xff", pos, stop)
    while candidate != -1:
        if _frame_pair_at(data, candidate, end, first) is not None:
            return candidate
        candidate = data.find(b"\xff", candidate + 1, stop)
    return None


def _carries_length_header(data: bytes, pos: int, frame: _Frame) -> bool:
    """Whether the frame at ``pos`` holds a Xing or Info header with a frame count, or a
    VBRI header: the cases where a tag reader's length is exact. The offsets are mutagen's."""
    if frame.layer != _LAYER3:
        return False
    if frame.version == _MPEG1:
        xing_offset = 21 if frame.mode == _MONO else 36
    else:
        xing_offset = 13 if frame.mode == _MONO else 21
    xing_at = pos + xing_offset
    if data[xing_at : xing_at + 4] in (b"Xing", b"Info"):
        flags = int.from_bytes(data[xing_at + 4 : xing_at + 8], "big")
        if flags & _XING_FRAMES_FLAG:
            return True
    vbri_at = pos + _VBRI_OFFSET
    return data[vbri_at : vbri_at + 4] == b"VBRI"


def _total_samples(data: bytes, pos: int, end: int, first: _Frame) -> int:
    """The samples in the frames from ``pos`` (the first frame) to ``end``."""
    samples = 0
    while pos < end:
        frame = _parse_frame(data, pos)
        if frame is None or not frame.continues(first):
            resynced = _sync(data, pos + 1, end, _RESYNC_WINDOW, first)
            if resynced is None:
                break
            pos = resynced
            continue
        if pos + frame.length > end:
            break  # a final frame cut short
        samples += frame.samples
        pos += frame.length
    return samples


def headerless_duration(data: bytes) -> float | None:
    """The duration in seconds of the MPEG audio in ``data``, summed from its frames.

    None when the first frame carries a length header (the tag reader's value is exact) or
    when no MPEG audio frames are found.
    """
    start, end = _audio_bounds(data)
    pos = _sync(data, start, end, _FIRST_SYNC_WINDOW, None)
    if pos is None:
        return None
    first = _parse_frame(data, pos)
    if first is None or _carries_length_header(data, pos, first):
        return None
    samples = _total_samples(data, pos, end, first)
    return samples / first.sample_rate if samples else None
