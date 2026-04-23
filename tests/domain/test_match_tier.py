from backend.domain.enums import MatchTier


def test_match_tier_has_musicbrainz_id_search() -> None:
    assert MatchTier.MUSICBRAINZ_ID_SEARCH.value == "musicbrainz_id_search"


def test_match_tier_has_local_file_fuzzy() -> None:
    assert MatchTier.LOCAL_FILE_FUZZY.value == "local_file_fuzzy"
