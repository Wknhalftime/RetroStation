from rapidfuzz.fuzz import token_sort_ratio

from backend.services.matching_utils import normalize_title_for_scoring, rule_matches


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
