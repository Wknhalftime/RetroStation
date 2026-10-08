"""Acceptance tests: a song D14 demoted is an ordinary review card (AUD-R025).

After migration 0036 the song is needs_review with its old match row kept, so the queue lists
its auto-matched artist, buckets the song by the kept score and shows the D14 reason:
- 56-64 is "needs_attention", with the kept file as the proposal;
- under 56 is "blocked" with no proposal. `likely` is per artist, so the artist is hidden by
  default only when it has no other likely song, as in this seed (a lone artist).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest

from backend.domain.enums import MatchStatus, MatchTier
from tests.routers.test_matching import (
    _insert_artist,
    _insert_identity,
    _insert_library_file,
    _insert_match_row,
)

QUEUE = "/api/v1/matching/queue"
_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "db"
    / "migrations"
    / "0036_song_midband_review.sql"
)


def _demoted_song(db_conn: psycopg.Connection[Any], score: float) -> tuple[str, str, str]:
    """An auto-matched artist with one song the old mid-band auto-matched at ``score``."""
    artist = _insert_artist(db_conn, "Review Artist", MatchStatus.AUTO_MATCHED)
    song = _insert_identity(db_conn, artist, "Panama", MatchStatus.AUTO_MATCHED)
    db_conn.execute(
        "UPDATE track_identities SET match_tier = %s WHERE id = %s",
        (MatchTier.MUSICBRAINZ_ID_EXACT.value, song.id),
    )
    kept = _insert_library_file(db_conn, track_title="Panama (Live)")
    _insert_match_row(
        db_conn, song, score, library_file_id=kept.id, match_tier="musicbrainz_id_exact"
    )
    db_conn.execute(_MIGRATION.read_text(encoding="utf-8"))
    db_conn.commit()
    return str(artist.id), str(song.id), str(kept.id)


def test_a_demoted_song_is_a_review_card_with_its_old_suggestion(client: Any, db_conn: Any) -> None:
    artist_id, song_id, kept_id = _demoted_song(db_conn, 60.0)

    data = client.get(QUEUE, params={"include_unlikely": "false"}).json()

    assert [a["id"] for a in data["items"]] == [artist_id]
    (song,) = data["items"][0]["identities"]
    assert song["id"] == song_id
    assert song["match_status"] == "needs_review"
    assert song["match_tier"] == "musicbrainz_id_exact"
    assert song["triage_bucket"] == "needs_attention"
    assert song["confidence_score"] == pytest.approx(60.0)
    assert song["reason_code"] == "LOW_CONFIDENCE"
    assert song["reason_detail"] == (
        "Score 60% — auto-matched by the old mid-band, back for review (D14)"
    )
    assert song["proposed_match"]["library_file_id"] == kept_id


def test_a_demoted_song_under_56_waits_for_the_rerun_hidden_by_default(
    client: Any, db_conn: Any
) -> None:
    """The old mid-band started at 55: under the 56 floor the song is "blocked" with no
    proposal until Re-run Matching floors it to auto_rejected (D6). Its lone artist has no
    likely song, so the default view hides it."""
    artist_id, _, _ = _demoted_song(db_conn, 55.0)

    hidden = client.get(QUEUE, params={"include_unlikely": "false"}).json()
    listed = client.get(QUEUE).json()

    assert (hidden["items"], hidden["unlikely_total"]) == ([], 1)
    (song,) = listed["items"][0]["identities"]
    assert listed["items"][0]["id"] == artist_id
    assert song["triage_bucket"] == "blocked"
    assert song["proposed_match"] is None
