from backend.services.normalization import strict_normalize, normalize_title


def test_strict_strips_hyphens() -> None:
    assert strict_normalize("Start-Me-Up") == "start me up"


def test_strict_strips_apostrophes() -> None:
    assert strict_normalize("Don't Stop") == "dont stop"


def test_strict_preserves_digits() -> None:
    assert strict_normalize("24K Magic") == "24k magic"


def test_strict_collapses_whitespace() -> None:
    assert strict_normalize("Hello   World") == "hello world"


def test_strict_empty_input() -> None:
    assert strict_normalize("") == ""


def test_year_removal_outside_parens() -> None:
    assert "hey jude" in normalize_title("Hey Jude 2011")


def test_year_only_title_preserved() -> None:
    result = normalize_title("1999")
    assert "1999" in result


def test_year_removal_hey_jude_2011_matches_hey_jude() -> None:
    assert normalize_title("Hey Jude 2011") == normalize_title("Hey Jude")
