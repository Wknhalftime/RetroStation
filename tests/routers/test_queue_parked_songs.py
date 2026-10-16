"""Acceptance: a parked song keeps its artist off the Resolution Center list (spec 2026-10-13 R4).

The list holds an artist when it, or one of its songs, is pending or needs_review. A song the
curator rejected is manual_rejected (parked), which is neither, until a re-check puts it back
in review with a guess at the presentation floor.
"""

from __future__ import annotations

import pytest

from backend.domain.enums import MatchStatus, ReasonCode
from backend.services.matching_constants import SONG_MIN_PRESENTATION_SCORE
from tests.routers.test_matching import _insert_artist, _insert_identity, _insert_match_row


def _listed(client, include_unlikely: bool) -> list[str]:
    flag = "true" if include_unlikely else "false"
    resp = client.get(f"/api/v1/matching/queue?include_unlikely={flag}")
    assert resp.status_code == 200
    return [item["id"] for item in resp.json()["items"]]


@pytest.mark.parametrize("include_unlikely", [True, False])
def test_an_artist_whose_only_open_song_is_parked_is_not_listed(
    client, db_conn, include_unlikely: bool
) -> None:
    artist = _insert_artist(db_conn, "Parked Artist", MatchStatus.AUTO_MATCHED)
    _insert_identity(db_conn, artist, "Rejected Song", MatchStatus.MANUAL_REJECTED)
    _insert_identity(db_conn, artist, "Matched Song", MatchStatus.AUTO_MATCHED)
    db_conn.commit()

    assert str(artist.id) not in _listed(client, include_unlikely)


def test_the_artist_returns_when_a_recheck_puts_the_song_back_in_review(client, db_conn) -> None:
    artist = _insert_artist(db_conn, "Parked Artist", MatchStatus.AUTO_MATCHED)
    song = _insert_identity(db_conn, artist, "Rejected Song", MatchStatus.MANUAL_REJECTED)
    db_conn.execute(
        "UPDATE track_identities SET match_status = %s, reason_code = %s WHERE id = %s",
        (MatchStatus.NEEDS_REVIEW.value, ReasonCode.LOW_CONFIDENCE.value, song.id),
    )
    _insert_match_row(db_conn, song, confidence_score=float(SONG_MIN_PRESENTATION_SCORE))
    db_conn.commit()

    assert _listed(client, include_unlikely=False) == [str(artist.id)]
