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
    assert mc.MIN_PRESENTATION_SCORE == 56  # artist model phase 1c (C35): the songs' floor
    assert mc.QUICK_REVIEW_MIN_SCORE == 65


def test_the_artist_floor_is_the_song_floor_and_the_mid_band_sits_under_quick_review() -> None:
    # From phase 1c (C35) the artist floor is the songs' 56, so it sits inside the old
    # mid-band, whose lead no longer auto-matches: a mid-band score is a suggestion.
    assert mc.MIN_PRESENTATION_SCORE == mc.SONG_MIN_PRESENTATION_SCORE
    assert mc.MID_BAND_LOWER < mc.MIN_PRESENTATION_SCORE <= mc.MID_BAND_UPPER
    assert mc.MID_BAND_UPPER < mc.QUICK_REVIEW_MIN_SCORE
    assert mc.MID_BAND_LOWER < mc.MID_BAND_UPPER
