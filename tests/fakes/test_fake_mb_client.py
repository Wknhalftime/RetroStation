from tests.fakes.mb_client import FakeMbClient


def test_fake_lookup_artist_records_call() -> None:
    client = FakeMbClient(artists={"mbid-A": {"id": "mbid-A", "name": "Alice"}})
    result = client.lookup_artist("mbid-A")
    assert result == {"id": "mbid-A", "name": "Alice"}
    assert "lookup_artist:mbid-A" in client.calls


def test_fake_lookup_artist_unknown_returns_none() -> None:
    client = FakeMbClient()
    assert client.lookup_artist("mbid-missing") is None
    assert client.calls == ["lookup_artist:mbid-missing"]
