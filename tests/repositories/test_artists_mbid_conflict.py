"""``upsert_musicbrainz_artist`` on PostgreSQL (artist model phase 2a, SPEC S5, S6; C41, D5).

Not locked. Before 2a a name another MBID held returned that MBID's artist. Since C41 every MBID
is its own artist: found by MBID, that artist; else the name's MBIDs decide, and none or only this
one promotes the one same-name name-only artist in place, while anything else inserts.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest
from structlog.testing import capture_logs

from backend.db.repositories.artists import PgArtistRepository

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[dict[str, Any]]

BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"
SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"


def _alice(repo: PgArtistRepository, mbid: str, *, disambiguation: str | None = "singer") -> str:
    return repo.upsert_musicbrainz_artist(
        mbid=mbid,
        name="Alice",
        sort_name="Alice, The",
        normalized_name="alice",
        disambiguation=disambiguation,
    )


def _file(conn: Conn, tag: str, *, present: bool = True) -> None:
    conn.execute(
        """INSERT INTO library_files
               (file_path, format, artist_name, normalized_artist_name, artist_mbid, file_status,
                missing_since)
           VALUES (%s, 'flac', 'Alice', 'alice', %s, %s, %s)""",
        (
            f"/music/{uuid4().hex}.flac",
            tag,
            "present" if present else "missing",
            None if present else "2026-10-01T00:00:00Z",
        ),
    )


def _row(conn: Conn, artist_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM artists WHERE id = %s", (artist_id,)).fetchone()
    assert row is not None
    return dict(row)


def test_upsert_same_mbid_returns_existing_id(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    first_id = _alice(repo, "mb-A")
    second_id = _alice(repo, "mb-A")
    assert first_id == second_id


def test_upsert_another_mbid_of_the_name_is_a_new_artist(pg_conn: Conn) -> None:
    """C41: the name is held by another MBID's artist. That artist is neither overwritten nor
    returned, and nothing warns: two acts may share a name."""
    repo = PgArtistRepository(pg_conn)
    original_id = _alice(repo, "mb-A")

    with capture_logs() as events:
        returned_id = _alice(repo, "mb-B")

    assert returned_id != original_id
    assert _row(pg_conn, original_id)["mbid"] == "mb-A"
    assert _row(pg_conn, returned_id)["mbid"] == "mb-B"
    assert [e for e in events if e["log_level"] in ("warning", "error")] == []


def test_promotes_the_name_only_artist_when_the_name_holds_no_mbid(pg_conn: Conn) -> None:
    """D5: no tag disagrees. It keeps its id and takes MusicBrainz's fields."""
    repo = PgArtistRepository(pg_conn)
    local = repo.upsert_local_artist("ALICE", "alice")

    assert _alice(repo, BAND, disambiguation=None) == local

    row = _row(pg_conn, local)
    assert (row["mbid"], row["origin"], row["name"], row["sort_name"]) == (
        BAND,
        "musicbrainz",
        "Alice",
        "Alice, The",
    )
    assert row["needs_enhancement"] is True


def test_promotes_the_name_only_artist_when_the_name_holds_only_this_mbid(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    local = repo.upsert_local_artist("Alice", "alice")
    _file(pg_conn, BAND)
    _file(pg_conn, BAND.upper())

    assert _alice(repo, BAND) == local


def test_a_promotion_with_complete_fields_clears_needs_enhancement(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    local = repo.upsert_local_artist("Alice", "alice")

    _alice(repo, BAND)

    assert _row(pg_conn, local)["needs_enhancement"] is False


def test_inserts_beside_the_name_only_artist_when_the_tags_name_another_mbid(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    local = repo.upsert_local_artist("Alice", "alice")
    _file(pg_conn, SINGER)

    returned = _alice(repo, BAND)

    assert returned != local
    assert _row(pg_conn, local)["mbid"] is None
    assert _row(pg_conn, returned)["origin"] == "musicbrainz"


def test_inserts_beside_the_name_only_artist_when_the_name_holds_several(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    local = repo.upsert_local_artist("Alice", "alice")
    _file(pg_conn, BAND)
    _file(pg_conn, SINGER)

    band, singer = _alice(repo, BAND), _alice(repo, SINGER)

    assert len({local, band, singer}) == 3
    assert _row(pg_conn, local)["mbid"] is None


def test_a_missing_file_does_not_hold_the_name(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    local = repo.upsert_local_artist("Alice", "alice")
    _file(pg_conn, SINGER, present=False)

    assert _alice(repo, BAND) == local


def test_upsert_complete_fields_clears_needs_enhancement(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-complete",
        name="Fully Known",
        sort_name="Known, Fully",
        normalized_name="fully known",
        disambiguation="Definitely the right one",
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row is not None
    assert row["needs_enhancement"] is False


def test_upsert_missing_disambiguation_keeps_needs_enhancement(pg_conn: Conn) -> None:
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-partial",
        name="Partial Artist",
        sort_name="Artist, Partial",
        normalized_name="partial artist",
        disambiguation=None,
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row is not None
    assert row["needs_enhancement"] is True


def test_upsert_sort_name_equals_name_keeps_needs_enhancement(pg_conn: Conn) -> None:
    """sort_name that merely echoes name isn't a real MB sort form."""
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-madonna",
        name="Madonna",
        sort_name="Madonna",
        normalized_name="madonna",
        disambiguation="American singer",
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row is not None
    assert row["needs_enhancement"] is True


def test_upsert_empty_disambiguation_keeps_needs_enhancement(pg_conn: Conn) -> None:
    """An empty-string disambiguation is effectively missing; still enqueue."""
    repo = PgArtistRepository(pg_conn)
    artist_id = repo.upsert_musicbrainz_artist(
        mbid="mb-empty-disambig",
        name="Blank",
        sort_name="Blank, First",
        normalized_name="blank",
        disambiguation="",
    )
    row = pg_conn.execute(
        "SELECT needs_enhancement FROM artists WHERE id = %s",
        (artist_id,),
    ).fetchone()
    assert row is not None
    assert row["needs_enhancement"] is True
