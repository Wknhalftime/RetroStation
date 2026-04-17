from backend.domain.synthetic_work_id import decode, encode


class TestEncode:
    def test_produces_syn_prefix(self) -> None:
        result = encode("artist-123", "My Song")
        assert result.startswith("syn_")

    def test_result_is_url_safe(self) -> None:
        result = encode("artist-123", "My Song (feat. X)")
        assert "+" not in result
        assert "/" not in result
        assert "=" not in result

    def test_different_inputs_produce_different_outputs(self) -> None:
        a = encode("artist-1", "Title A")
        b = encode("artist-1", "Title B")
        assert a != b


class TestDecode:
    def test_round_trip(self) -> None:
        artist_id = "550e8400-e29b-41d4-a716-446655440000"
        title = "Some Track Title"
        encoded = encode(artist_id, title)
        result = decode(encoded)
        assert result == (artist_id, title)

    def test_returns_none_for_non_synthetic_id(self) -> None:
        assert decode("mb_some-mbid-here") is None
        assert decode("plain-string") is None

    def test_handles_colon_in_title(self) -> None:
        artist_id = "artist-abc"
        title = "Part 1: The Beginning"
        encoded = encode(artist_id, title)
        result = decode(encoded)
        assert result == (artist_id, title)

    def test_returns_none_for_corrupted_input(self) -> None:
        assert decode("syn_notvalidbase64!!!") is None
