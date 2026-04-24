from tests.fakes.artists import FakeArtistRepository


def test_list_unenhanced_excludes_failed_rows() -> None:
    """Artists with a logged enhancement_error are NOT re-queued."""
    repo = FakeArtistRepository()
    ok_id = repo.upsert_musicbrainz_artist(
        mbid="mb-ok",
        name="OK Artist",
        sort_name="OK Artist",
        normalized_name="ok artist",
        disambiguation=None,
    )
    failed_id = repo.upsert_musicbrainz_artist(
        mbid="mb-bad",
        name="Bad MBID",
        sort_name="Bad MBID",
        normalized_name="bad mbid",
        disambiguation=None,
    )
    repo.mark_enhancement_failed(failed_id, "404 from MB")

    ids = [a.id for a in repo.list_unenhanced()]
    assert ok_id in ids
    assert failed_id not in ids
