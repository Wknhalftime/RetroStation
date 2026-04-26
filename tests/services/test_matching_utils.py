from rapidfuzz.fuzz import token_sort_ratio

from backend.domain.enums import ReasonCode
from backend.services.matching_reasons import format_deferred_retry
from backend.services.matching_utils import (
    TRUNCATION_TOLERANCE_CHARS,
    is_likely_truncated,
    normalize_title_for_scoring,
    rule_matches,
)


class TestRuleMatches:
    def test_exact_string_match(self) -> None:
        assert rule_matches("prince", "prince") is True

    def test_regex_match(self) -> None:
        assert rule_matches(r"prince.*", "prince and the revolution") is True

    def test_miss(self) -> None:
        assert rule_matches("prince", "madonna") is False

    def test_invalid_regex_returns_false_without_raising(self) -> None:
        # Unbalanced parenthesis: invalid regex. Must not raise re.error.
        assert rule_matches("prince(", "prince(") is True  # exact match short-circuits
        assert rule_matches("prince(", "prince") is False  # falls to re.error path


class TestNormalizeTitleForScoring:
    def test_strip_live_suffix(self) -> None:
        assert normalize_title_for_scoring("Purple Rain (Live)") == "Purple Rain"

    def test_strip_feat_clause_parenthesised(self) -> None:
        assert normalize_title_for_scoring("Song (feat. Artist)") == "Song"

    def test_strip_feat_clause_bare(self) -> None:
        assert normalize_title_for_scoring("Song feat. Artist") == "Song"

    def test_strip_radio_edit(self) -> None:
        assert normalize_title_for_scoring("Song (Radio Edit)") == "Song"

    def test_strip_remaster(self) -> None:
        assert normalize_title_for_scoring("Song (Remastered)") == "Song"

    def test_idempotent_plain_title(self) -> None:
        assert normalize_title_for_scoring("Song Title") == "Song Title"

    def test_case_insensitive(self) -> None:
        assert normalize_title_for_scoring("Song (LIVE)") == "Song"
        assert normalize_title_for_scoring("Song (live)") == "Song"

    def test_normalized_live_variant_scores_100_against_plain(self) -> None:
        a = normalize_title_for_scoring("Purple Rain")
        b = normalize_title_for_scoring("Purple Rain (Live)")
        assert token_sort_ratio(a, b) == 100

    def test_brackets_instead_of_parens(self) -> None:
        assert normalize_title_for_scoring("Song [Live]") == "Song"


class TestIsLikelyTruncated:
    def test_short_clean_name_returns_false(self) -> None:
        assert is_likely_truncated("U2", max_len=30) is False

    def test_medium_clean_name_returns_false(self) -> None:
        assert is_likely_truncated("Florence + The Machine", max_len=30) is False

    def test_at_limit_alphanumeric_end_returns_true(self) -> None:
        name = "A" * 30
        assert is_likely_truncated(name, max_len=30) is True

    def test_within_tolerance_alphanumeric_end_returns_true(self) -> None:
        # max_len - TRUNCATION_TOLERANCE_CHARS, alphanumeric end → suspicious.
        name = "B" * (30 - TRUNCATION_TOLERANCE_CHARS)
        assert is_likely_truncated(name, max_len=30) is True

    def test_at_limit_punctuation_end_returns_false(self) -> None:
        name = "C" * 29 + "."
        assert is_likely_truncated(name, max_len=30) is False

    def test_at_limit_digit_end_returns_true(self) -> None:
        # Digits count as alphanumeric → still suspicious.
        name = "Blink-18" + "X" * 21 + "2"
        assert len(name) == 30
        assert is_likely_truncated(name, max_len=30) is True

    def test_well_below_limit_returns_false(self) -> None:
        assert is_likely_truncated("Queen", max_len=30) is False

    def test_empty_string_returns_false(self) -> None:
        assert is_likely_truncated("", max_len=30) is False

    def test_tolerance_constant_value(self) -> None:
        # Locks the published value — change requires updating callers.
        assert TRUNCATION_TOLERANCE_CHARS == 2


class TestDeferredRetryReason:
    def test_enum_value_is_stable_string(self) -> None:
        assert ReasonCode.DEFERRED_RETRY == "DEFERRED_RETRY"

    def test_format_deferred_retry_message_mentions_retry(self) -> None:
        msg = format_deferred_retry()
        assert "deferred" in msg.lower()
        assert "retry" in msg.lower()
        assert "next playlist" in msg.lower()
