"""Acceptance (spec 2026-10-13 R7): an artist with nothing to review stays reachable in the
hidden "no likely match" list and never appears in the main list.

A matched artist whose songs are only auto_rejected / manual_rejected (besides matched ones)
is listed only with include_unlikely=true and counts in unlikely_total. An artist that is
itself auto_rejected is not added: its songs sit behind "Resolve the artist first".
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.domain.enums import MatchStatus
from tests.routers.test_matching import _insert_artist, _insert_identity


def _queue(client, include_unlikely: bool) -> dict[str, Any]:
    flag = "true" if include_unlikely else "false"
    resp = client.get(f"/api/v1/matching/queue?include_unlikely={flag}")
    assert resp.status_code == 200
    body: dict[str, Any] = resp.json()
    return body


def _ids(body: dict[str, Any]) -> list[str]:
    return [item["id"] for item in body["items"]]


@pytest.mark.parametrize("song_status", [MatchStatus.AUTO_REJECTED, MatchStatus.MANUAL_REJECTED])
@pytest.mark.parametrize("artist_status", [MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED])
def test_a_matched_artist_with_only_rejected_songs_is_hidden_but_reachable(
    client, db_conn, artist_status: MatchStatus, song_status: MatchStatus
) -> None:
    artist = _insert_artist(db_conn, "Nothing To Review", artist_status)
    _insert_identity(db_conn, artist, "Rejected Song", song_status)
    _insert_identity(db_conn, artist, "Matched Song", MatchStatus.AUTO_MATCHED)
    db_conn.commit()

    default = _queue(client, include_unlikely=False)
    assert str(artist.id) not in _ids(default)
    assert default["unlikely_total"] == 1

    assert str(artist.id) in _ids(_queue(client, include_unlikely=True))


def test_an_auto_rejected_artist_with_auto_rejected_songs_is_not_listed(client, db_conn) -> None:
    artist = _insert_artist(db_conn, "No MusicBrainz Artist", MatchStatus.AUTO_REJECTED)
    _insert_identity(db_conn, artist, "Some Song", MatchStatus.AUTO_REJECTED)
    db_conn.commit()

    everything = _queue(client, include_unlikely=True)
    assert str(artist.id) not in _ids(everything)
    assert everything["unlikely_total"] == 0


def test_a_matched_artist_with_only_matched_songs_is_not_listed(client, db_conn) -> None:
    artist = _insert_artist(db_conn, "All Done", MatchStatus.AUTO_MATCHED)
    _insert_identity(db_conn, artist, "Matched Song", MatchStatus.AUTO_MATCHED)
    db_conn.commit()

    assert str(artist.id) not in _ids(_queue(client, include_unlikely=True))
