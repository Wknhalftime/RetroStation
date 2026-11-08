"""``track_identities.version_text`` (versions phase 2b, SPEC S1; design C59; AUD-R034).

A log line's version text is its title's trailing bracket groups: round or square, each group's
text lowercased with its runs of whitespace made one space and trimmed, the groups joined in
order by one space. A title with no trailing group, or only empty ones, has none (NULL). Dash
suffixes are ignored: none of the log's 31,259 titles used one for a version on 10-28 (C59).

The column is generated and stored over ``original_title`` (migration 0050), so a line has it
from the moment it exists, whoever writes the line, and the lines already there are filled when
the column is made. It is indexed, for the view's version join (S4).
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.broadcast_track_identities import PgBroadcastTrackIdentityRepository
from backend.domain.broadcast import BroadcastTrackIdentity
from backend.domain.enums import MatchStatus
from tests.integration import stream_seed as seed
from tests.integration.stream_seed import Conn

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def version_text(conn: Conn, identity_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT version_text FROM track_identities WHERE id = %s", (identity_id,)
    ).fetchone()
    assert row is not None
    return row["version_text"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Enter Sandman (Live S&M)", "live s&m"),
        ("Paradise City ('08 Mix)", "'08 mix"),
        ("Enter Sandman (Lv)", "lv"),
        ("Moonlight Sonata (Part 1) (Live)", "part 1 live"),
        ("Enter Sandman", None),
        ("Black [Unplugged]", "unplugged"),
        ("Black (Live) [Explicit]", "live explicit"),
        ("Black (Live)(Acoustic)", "live acoustic"),
        ("Black (LIVE)", "live"),
        ("Black ( Live   at  Wembley )", "live at wembley"),
        ("Black (Live)  ", "live"),
        ("Happy Xmas (War Is Over)", "war is over"),
        ("Brass in Pocket (I'm Special)", "i'm special"),
        ("(I Can't Get No) Satisfaction", None),
        ("Black (Live) Remastered", None),
        ("Enter Sandman - Live", None),
        ("Enter Sandman ()", None),
    ],
)
def test_the_version_text_of_a_title(conn: Conn, title: str, expected: str | None) -> None:
    """The real shapes ("(Live S&M)", "('08 Mix)", "(Lv)", "(Part 1) (Live)", none), and the
    rule's edges: square brackets, several groups, case, spaces, a leading or inner group, a dash
    suffix, an empty group."""
    line = seed.identity(conn, title=title)

    assert version_text(conn, line) == expected


def test_a_line_the_ingest_stores_has_its_version_text(conn: Conn) -> None:
    """The identity upsert names no version text: the column fills itself."""
    artist = conn.execute(
        """INSERT INTO broadcast_artists (original_name, normalized_name)
           VALUES ('Metallica', 'metallica') RETURNING id"""
    ).fetchone()
    assert artist is not None
    stored = PgBroadcastTrackIdentityRepository(conn).upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=UUID(str(artist["id"])),
            original_title="Enter Sandman (Live S&M)",
            normalized_title="enter sandman (live s&m)",
            normalized_signature=f"metallica|enter sandman (live s&m)|{uuid4().hex}",
            match_status=MatchStatus.PENDING,
        )
    )

    assert version_text(conn, stored.id) == "live s&m"


def test_the_version_text_follows_its_title(conn: Conn) -> None:
    line = seed.identity(conn, title="Enter Sandman (Live)")

    conn.execute(
        "UPDATE track_identities SET original_title = 'Enter Sandman (Lv)' WHERE id = %s",
        (line,),
    )

    assert version_text(conn, line) == "lv"


def test_the_version_text_cannot_be_written(conn: Conn) -> None:
    line = seed.identity(conn, title="Enter Sandman (Live)")

    with pytest.raises(psycopg.errors.GeneratedAlways), conn.transaction():
        conn.execute("UPDATE track_identities SET version_text = 'lv' WHERE id = %s", (line,))
    with pytest.raises(psycopg.errors.GeneratedAlways), conn.transaction():
        conn.execute(
            """INSERT INTO track_identities
                   (broadcast_artist_id, original_title, normalized_title,
                    normalized_signature, version_text)
               SELECT broadcast_artist_id, 'Black (Live)', 'black (live)', %s, 'live'
               FROM track_identities WHERE id = %s""",
            (uuid4().hex, line),
        )

    assert version_text(conn, line) == "live"


def test_the_column_is_stored_and_indexed(conn: Conn) -> None:
    generated = conn.execute(
        """SELECT a.attgenerated
           FROM pg_attribute a
           WHERE a.attrelid = 'track_identities'::regclass AND a.attname = 'version_text'
             AND NOT a.attisdropped"""
    ).fetchone()
    indexed = conn.execute(
        """SELECT count(*) AS n
           FROM pg_index i
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
           WHERE i.indrelid = 'track_identities'::regclass
             AND a.attname = 'version_text'"""
    ).fetchone()

    assert generated is not None and generated["attgenerated"] == "s"
    assert indexed is not None and indexed["n"] >= 1
