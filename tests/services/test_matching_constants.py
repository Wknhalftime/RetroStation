from backend.services import matching_constants as mc


def test_constants_are_integers() -> None:
    assert isinstance(mc.MB_AUTO_LINK_SCORE, int)
    assert isinstance(mc.MB_SCORE_GAP, int)
    assert isinstance(mc.MID_BAND_LOWER, int)
    assert isinstance(mc.MID_BAND_UPPER, int)
    assert isinstance(mc.MID_BAND_GAP_THRESHOLD, int)
    assert isinstance(mc.MIN_PRESENTATION_SCORE, int)
    assert isinstance(mc.QUICK_REVIEW_MIN_SCORE, int)


def test_canonical_values() -> None:
    # These values are canonical per plan §3.3 and §11.1. Changing any of them
    # will change matcher behavior — only change with explicit product sign-off.
    assert mc.MB_AUTO_LINK_SCORE == 95
    assert mc.MB_SCORE_GAP == 10
    assert mc.MID_BAND_LOWER == 55
    assert mc.MID_BAND_UPPER == 64
    assert mc.MID_BAND_GAP_THRESHOLD == 5
    assert mc.MIN_PRESENTATION_SCORE == 50
    assert mc.QUICK_REVIEW_MIN_SCORE == 65


def test_mid_band_is_within_noise_floor_and_high_threshold() -> None:
    assert mc.MIN_PRESENTATION_SCORE <= mc.MID_BAND_LOWER
    assert mc.MID_BAND_UPPER < mc.QUICK_REVIEW_MIN_SCORE
    assert mc.MID_BAND_LOWER < mc.MID_BAND_UPPER
