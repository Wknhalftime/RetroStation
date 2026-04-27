from backend.services.matching_reasons import (
    ReasonCode,
    format_ambiguous_gap,
    format_low_confidence,
)


class TestReasonCode:
    def test_all_expected_members_exist(self) -> None:
        assert ReasonCode.LOW_CONFIDENCE.value == "LOW_CONFIDENCE"
        assert ReasonCode.AMBIGUOUS_GAP.value == "AMBIGUOUS_GAP"
        assert ReasonCode.NO_CANDIDATES.value == "NO_CANDIDATES"
        assert ReasonCode.NO_LOCAL_FILES.value == "NO_LOCAL_FILES"
        assert ReasonCode.MB_SEARCH_INCONCLUSIVE.value == "MB_SEARCH_INCONCLUSIVE"
        assert ReasonCode.MISSING_MATCH_RECORD.value == "MISSING_MATCH_RECORD"

    def test_values_are_stable_strings(self) -> None:
        # Values are persisted to DB; renaming breaks telemetry and regressions.
        for member in ReasonCode:
            assert member.value == member.name, (
                f"ReasonCode.{member.name} value must equal name for DB stability"
            )

    def test_members_are_str(self) -> None:
        # We subclass str so DB writes can use the enum directly.
        assert isinstance(ReasonCode.LOW_CONFIDENCE, str)


class TestFormatLowConfidence:
    def test_rounds_to_zero_decimals(self) -> None:
        assert format_low_confidence(72.4) == "Score 72% \u2014 below confidence threshold"

    def test_rounds_half_up(self) -> None:
        assert format_low_confidence(72.6) == "Score 73% \u2014 below confidence threshold"

    def test_zero(self) -> None:
        assert format_low_confidence(0.0) == "Score 0% \u2014 below confidence threshold"

    def test_half_rounds_up_not_bankers(self) -> None:
        # Locks in round-half-up; f"{:.0f}" would render 72.5 as 72 (banker's).
        assert format_low_confidence(72.5) == "Score 73% \u2014 below confidence threshold"
        assert format_low_confidence(73.5) == "Score 74% \u2014 below confidence threshold"


class TestFormatAmbiguousGap:
    def test_typical(self) -> None:
        assert (
            format_ambiguous_gap(3.2, 10.0)
            == "Top candidates within 3 points (gap < 10 required)"
        )

    def test_zero_gap(self) -> None:
        assert (
            format_ambiguous_gap(0.0, 10.0)
            == "Top candidates within 0 points (gap < 10 required)"
        )



