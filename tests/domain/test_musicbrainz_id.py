from __future__ import annotations

import pytest

from backend.domain.catalog import InvalidMusicBrainzIdError, MusicBrainzId

_CANONICAL = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"


@pytest.mark.parametrize("raw", [_CANONICAL, _CANONICAL.upper()])
def test_parse_accepts_forms_musicbrainz_accepts(raw: str) -> None:
    # Probed live against /ws/2/artist/: both cases return 200.
    parsed = MusicBrainzId.parse(raw)

    assert parsed is not None
    assert parsed.value == raw


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-uuid",
        "",
        f" {_CANONICAL}",  # MusicBrainz 400s on surrounding whitespace
        f"{{{_CANONICAL}}}",  # ...on braces
        _CANONICAL.replace("-", ""),  # ...and on the unhyphenated form
        _CANONICAL[:-1] + "g",
        f"{_CANONICAL}\n",
    ],
)
def test_parse_rejects_forms_musicbrainz_answers_with_400(raw: str) -> None:
    assert MusicBrainzId.parse(raw) is None


def test_direct_construction_with_malformed_value_raises() -> None:
    with pytest.raises(InvalidMusicBrainzIdError):
        MusicBrainzId("not-a-uuid")


def test_is_immutable() -> None:
    mbid = MusicBrainzId(_CANONICAL)

    with pytest.raises(AttributeError):
        mbid.value = "other"  # type: ignore[misc]
