from __future__ import annotations

from backend.services.master_selection_service import (
    FORMAT_BONUS,
    RELEASE_STATUS_SCORE,
    RELEASE_TYPE_SCORE,
    score_file_row,
)


class TestScoreFileRow:
    def _row(
        self,
        release_status: str | None = None,
        release_type: str | None = None,
        fmt: str | None = None,
        bitrate: int | None = None,
        duration_ms: int | None = None,
    ) -> dict:
        return {
            "release_status": release_status,
            "release_type": release_type,
            "format": fmt,
            "bitrate": bitrate,
            "duration_ms": duration_ms,
        }

    def test_all_none_returns_format_default(self) -> None:
        score, bitrate, duration = score_file_row(self._row())
        assert score == 1  # FORMAT_BONUS default for unknown format
        assert bitrate == 0
        assert duration == 0

    def test_promotion_outscores_official(self) -> None:
        promotion = score_file_row(self._row(release_status="promotion"))
        official = score_file_row(self._row(release_status="official"))
        assert promotion[0] > official[0]

    def test_album_outscores_single(self) -> None:
        album = score_file_row(self._row(release_type="album"))
        single = score_file_row(self._row(release_type="single"))
        assert album[0] > single[0]

    def test_unknown_release_type_uses_other_fallback(self) -> None:
        other = score_file_row(self._row(release_type="other"))
        unknown = score_file_row(self._row(release_type="bootleg"))
        assert other[0] == unknown[0]

    def test_flac_outscores_mp3(self) -> None:
        flac = score_file_row(self._row(fmt="flac"))
        mp3 = score_file_row(self._row(fmt="mp3"))
        assert flac[0] > mp3[0]

    def test_format_comparison_is_case_insensitive(self) -> None:
        lower = score_file_row(self._row(fmt="flac"))
        upper = score_file_row(self._row(fmt="FLAC"))
        assert lower[0] == upper[0]

    def test_bitrate_and_duration_passed_through(self) -> None:
        _, bitrate, duration = score_file_row(
            self._row(bitrate=320, duration_ms=240000)
        )
        assert bitrate == 320
        assert duration == 240000

    def test_none_bitrate_and_duration_become_zero(self) -> None:
        _, bitrate, duration = score_file_row(self._row(bitrate=None, duration_ms=None))
        assert bitrate == 0
        assert duration == 0

    def test_full_score_accumulates_all_components(self) -> None:
        row = self._row(release_status="official", release_type="album", fmt="flac")
        score, _, _ = score_file_row(row)
        expected = (
            RELEASE_STATUS_SCORE["official"]
            + RELEASE_TYPE_SCORE["album"]
            + FORMAT_BONUS["flac"]
        )
        assert score == expected

    def test_max_selects_highest_scored_row(self) -> None:
        rows = [
            self._row(release_status="official", release_type="single", fmt="mp3"),
            self._row(release_status="official", release_type="album", fmt="flac"),
            self._row(release_status="promotion", release_type="album", fmt="flac"),
        ]
        best = max(rows, key=score_file_row)
        assert best["release_status"] == "promotion"

    def test_tie_on_score_is_broken_by_bitrate(self) -> None:
        # Identical scoring components -> tie on the first tuple element;
        # higher bitrate must win as the second tuple element.
        rows = [
            self._row(release_status="official", release_type="album", fmt="flac", bitrate=320),
            self._row(release_status="official", release_type="album", fmt="flac", bitrate=128),
        ]
        best = max(rows, key=score_file_row)
        assert best["bitrate"] == 320

    def test_tie_on_score_and_bitrate_is_broken_by_duration(self) -> None:
        # Identical score AND bitrate -> tie on the first two elements;
        # higher duration must win as the third tuple element.
        rows = [
            self._row(
                release_status="official", release_type="album", fmt="flac",
                bitrate=320, duration_ms=180000,
            ),
            self._row(
                release_status="official", release_type="album", fmt="flac",
                bitrate=320, duration_ms=240000,
            ),
        ]
        best = max(rows, key=score_file_row)
        assert best["duration_ms"] == 240000

    def test_constants_match_across_both_call_sites(self) -> None:
        # Verify the three score dicts are the authoritative copies used by score_file_row.
        assert RELEASE_STATUS_SCORE["promotion"] == 100
        assert RELEASE_STATUS_SCORE["official"] == 0
        assert RELEASE_TYPE_SCORE["album"] == 80
        assert FORMAT_BONUS["flac"] == 10
