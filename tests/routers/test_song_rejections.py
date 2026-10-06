"""Acceptance tests: Reject and Unmatch record (song, file) rejections
(spec 2026-10-05 §4.1).

Status matrix for Reject: unknown id 404; pending / needs_review need the shown
file (422 without it); auto_matched / manual_matched record every non-null
matched file and ignore the body; auto_rejected / manual_rejected give 409. Both
actions leave the song needs_review / USER_UNMATCHED with tier and detail
cleared and its match rows deleted (D3, D4, D5, D8).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from backend.domain.enums import MatchStatus, ReasonCode
from tests.routers.test_matching import (
    _insert_identity,
    _insert_library_file,
    _insert_match_row,
    _seed_review_chain,
)

REJECT = {"match_status": "manual_rejected"}


def _song(db_conn: psycopg.Connection[Any], status: MatchStatus) -> Any:
    _, _, artist, _, _ = _seed_review_chain(db_conn)
    return _insert_identity(db_conn, artist, original_title="Shown Song", match_status=status)


def _row(db_conn: psycopg.Connection[Any], identity_id: UUID) -> dict[str, Any]:
    row = db_conn.execute(
        """SELECT match_status, match_tier, reason_code, reason_detail,
           rejected_file_ids FROM track_identities WHERE id = %s""",
        (identity_id,),
    ).fetchone()
    assert row is not None
    return row


def _match_rows(db_conn: psycopg.Connection[Any], identity_id: UUID) -> int:
    row = db_conn.execute(
        "SELECT count(*) AS n FROM matches WHERE identity_id = %s",
        (identity_id,),
    ).fetchone()
    assert row is not None
    return int(row["n"])


def _assert_back_in_review(db_conn: psycopg.Connection[Any], identity_id: UUID) -> dict[str, Any]:
    row = _row(db_conn, identity_id)
    assert row["match_status"] == MatchStatus.NEEDS_REVIEW.value
    assert row["reason_code"] == ReasonCode.USER_UNMATCHED.value
    assert row["match_tier"] is None
    assert row["reason_detail"] is None
    assert _match_rows(db_conn, identity_id) == 0
    return row


def _reject(client: Any, identity_id: UUID, file_id: UUID | None) -> Any:
    body: dict[str, Any] = dict(REJECT)
    if file_id is not None:
        body["library_file_id"] = str(file_id)
    return client.post(f"/api/v1/matching/identities/{identity_id}/resolve", json=body)


# --- Reject: a song in review ----


@pytest.mark.parametrize("status", [MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW])
def test_reject_records_the_shown_suggestion(client, db_conn, status: MatchStatus) -> None:
    song = _song(db_conn, status)
    shown = _insert_library_file(db_conn, track_title="Shown Song")
    _insert_match_row(db_conn, song, 70.0, library_file_id=shown.id)
    db_conn.commit()

    resp = _reject(client, song.id, shown.id)

    assert resp.status_code == 200
    assert resp.json()["match_status"] == MatchStatus.NEEDS_REVIEW.value
    row = _assert_back_in_review(db_conn, song.id)
    assert list(row["rejected_file_ids"]) == [shown.id]


@pytest.mark.parametrize("status", [MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW])
def test_reject_in_review_without_a_file_is_422_and_changes_nothing(
    client, db_conn, status: MatchStatus
) -> None:
    song = _song(db_conn, status)
    shown = _insert_library_file(db_conn)
    _insert_match_row(db_conn, song, 70.0, library_file_id=shown.id)
    db_conn.commit()

    resp = _reject(client, song.id, None)

    assert resp.status_code == 422
    row = _row(db_conn, song.id)
    assert row["match_status"] == status.value
    assert list(row["rejected_file_ids"]) == []
    assert _match_rows(db_conn, song.id) == 1


def test_reject_records_a_file_id_the_library_no_longer_has(client, db_conn) -> None:
    song = _song(db_conn, MatchStatus.NEEDS_REVIEW)
    gone = uuid4()

    resp = _reject(client, song.id, gone)

    assert resp.status_code == 200
    assert list(_row(db_conn, song.id)["rejected_file_ids"]) == [gone]


def test_rejecting_the_same_file_twice_records_it_once(client, db_conn) -> None:
    song = _song(db_conn, MatchStatus.NEEDS_REVIEW)
    shown = _insert_library_file(db_conn)
    db_conn.commit()

    assert _reject(client, song.id, shown.id).status_code == 200
    assert _reject(client, song.id, shown.id).status_code == 200

    assert list(_row(db_conn, song.id)["rejected_file_ids"]) == [shown.id]


def test_reject_keeps_earlier_rejections(client, db_conn) -> None:
    song = _song(db_conn, MatchStatus.NEEDS_REVIEW)
    earlier, shown = uuid4(), _insert_library_file(db_conn)
    db_conn.execute(
        "UPDATE track_identities SET rejected_file_ids = %s WHERE id = %s",
        ([earlier], song.id),
    )
    db_conn.commit()

    assert _reject(client, song.id, shown.id).status_code == 200

    assert set(_row(db_conn, song.id)["rejected_file_ids"]) == {earlier, shown.id}


# --- Reject: a matched song ----


@pytest.mark.parametrize("status", [MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED])
def test_reject_matched_song_records_every_matched_file_and_ignores_the_body(
    client, db_conn, status: MatchStatus
) -> None:
    song = _song(db_conn, status)
    first, second = _insert_library_file(db_conn), _insert_library_file(db_conn)
    _insert_match_row(db_conn, song, 99.0, library_file_id=first.id)
    _insert_match_row(db_conn, song, 98.0, library_file_id=second.id)
    _insert_match_row(db_conn, song, 50.0, library_file_id=None)
    db_conn.commit()

    resp = _reject(client, song.id, uuid4())

    assert resp.status_code == 200
    row = _assert_back_in_review(db_conn, song.id)
    assert set(row["rejected_file_ids"]) == {first.id, second.id}


def test_reject_matched_song_without_a_file_row_still_returns_it_to_review(client, db_conn) -> None:
    song = _song(db_conn, MatchStatus.AUTO_MATCHED)
    _insert_match_row(db_conn, song, 99.0, library_file_id=None)
    db_conn.commit()

    assert _reject(client, song.id, None).status_code == 200

    row = _assert_back_in_review(db_conn, song.id)
    assert list(row["rejected_file_ids"]) == []


# --- Reject: refused ----


@pytest.mark.parametrize("status", [MatchStatus.AUTO_REJECTED, MatchStatus.MANUAL_REJECTED])
def test_reject_on_a_rejected_song_is_409(client, db_conn, status: MatchStatus) -> None:
    song = _song(db_conn, status)

    resp = _reject(client, song.id, uuid4())

    assert resp.status_code == 409
    row = _row(db_conn, song.id)
    assert row["match_status"] == status.value
    assert list(row["rejected_file_ids"]) == []


def test_reject_unknown_song_is_404_before_any_file_check(client) -> None:
    assert _reject(client, uuid4(), None).status_code == 404


# --- Unmatch ----


@pytest.mark.parametrize("status", [MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED])
def test_unmatch_matched_song_records_its_file(client, db_conn, status: MatchStatus) -> None:
    song = _song(db_conn, status)
    matched = _insert_library_file(db_conn)
    _insert_match_row(db_conn, song, 99.0, library_file_id=matched.id)
    db_conn.commit()

    resp = client.post(f"/api/v1/matching/identities/{song.id}/unmatch")

    assert resp.status_code == 200
    assert resp.json()["match_status"] == MatchStatus.NEEDS_REVIEW.value
    row = _assert_back_in_review(db_conn, song.id)
    assert list(row["rejected_file_ids"]) == [matched.id]


def test_unmatch_auto_rejected_song_records_nothing(client, db_conn) -> None:
    song = _song(db_conn, MatchStatus.AUTO_REJECTED)
    weak_guess = _insert_library_file(db_conn)
    _insert_match_row(db_conn, song, 30.0, library_file_id=weak_guess.id)
    db_conn.commit()

    assert client.post(f"/api/v1/matching/identities/{song.id}/unmatch").status_code == 200

    row = _assert_back_in_review(db_conn, song.id)
    assert list(row["rejected_file_ids"]) == []


def test_unmatch_legacy_manual_rejected_frees_it_without_recording(client, db_conn) -> None:
    song = _song(db_conn, MatchStatus.MANUAL_REJECTED)

    assert client.post(f"/api/v1/matching/identities/{song.id}/unmatch").status_code == 200

    row = _assert_back_in_review(db_conn, song.id)
    assert list(row["rejected_file_ids"]) == []


@pytest.mark.parametrize("status", [MatchStatus.PENDING, MatchStatus.NEEDS_REVIEW])
def test_unmatch_in_review_is_still_409(client, db_conn, status: MatchStatus) -> None:
    song = _song(db_conn, status)

    assert client.post(f"/api/v1/matching/identities/{song.id}/unmatch").status_code == 409
