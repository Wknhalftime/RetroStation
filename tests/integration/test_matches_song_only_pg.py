"""Migration 0046 drops the old artist rows of ``matches`` and C34's columns (artist model phase
1c, SPEC S10, S11; M35, C34, C70).

S10: since 1a the log names' links live in ``log_name_links``, and the artist rows 0044 copied
there were 1a's and 1b's rollback net. 0046 deletes every artist row of ``matches`` and drops
the artist column with its CHECK (``xor_match_target``), its UNIQUE
(``matches_artist_id_target_id_key``), its foreign key and its index (``idx_matches_artist``);
``matches.identity_id`` becomes NOT NULL, so a match is a song's. ``link_copy_stubs``, 0044's
list of its stubs for its rollback, goes. S11: ``broadcast_artists.artist_candidates`` and
``broadcast_artists.resolved_artist_mbid`` go; the second exists only on the dev database, so
every drop uses IF EXISTS. Song rows are untouched. The domain follows: ``Match`` has no artist
column and needs its song, and ``get_by_artist`` leaves ``MatchRepository``, its PostgreSQL
class and its fake (``WorkRepository.get_by_artist`` is another method and stays).

``backend/db/rollback_0046_drop_artist_rows.sql`` puts back the column, the CHECK, the UNIQUE,
the foreign key, the index, an empty ``link_copy_stubs`` and an empty ``artist_candidates``
(never written; ``resolved_artist_mbid`` is read by nothing and stays gone), makes
``identity_id`` nullable again, and restores one artist row for each v0 copy of one
(``evidence.legacy``: ``match_id``, ``target_id``, tier, score, ``created_at``): each link's,
whatever its state, and each link 0045 deleted, whose copy ``link_demotion_backup`` keeps until
Lance accepts 1c. It deletes 0046's ``schema_migrations`` row. A link written since 1a holds no
copy and gets no row; a duplicate row 0044 collapsed into one link stays gone. The rollbacks run
newest first: 0046's, then 0045's, then 0044's (which needs the column and ``link_copy_stubs``
back).

Each database test but the schema's runs on the database as it was before 0046: the rollback
undoes 0046 inside one transaction, the test seeds rows as 0044, 0045 and 1a's writers left them
and runs the migration's SQL, and the transaction is rolled back at the end.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.matches import PgMatchRepository
from backend.domain.enums import MatchTier
from backend.domain.matching import Match
from backend.repositories.matches import MatchRepository
from tests.fakes.matches import FakeMatchRepository

Conn = psycopg.Connection[Any]

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0046_drop_artist_rows"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"
ROLLBACK_0045 = ROOT / "backend" / "db" / "rollback_0045_link_demotion.sql"
ROLLBACK_0044 = ROOT / "backend" / "db" / "rollback_0044_log_name_links.sql"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
ROW_COLUMNS = ("id", "artist_id", "target_id", "target_type", "confidence_score", "match_tier")
SONG_COLUMNS = (
    "id",
    "identity_id",
    "library_file_id",
    "target_id",
    "target_type",
    "confidence_score",
    "match_tier",
    "work_id",
    "trace_id",
    "created_at",
)


@contextmanager
def before_the_drop(url: str) -> Iterator[Conn]:
    """The test database without 0046, in one transaction that is rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _migrate(conn: Conn) -> None:
    conn.execute(MIGRATION.read_text(encoding="utf-8"))


def _roll_back(conn: Conn) -> None:
    conn.execute(ROLLBACK.read_text(encoding="utf-8"))


# --- seeds ---------------------------------------------------------------------------------------


def _name(conn: Conn, spelling: str, status: str = "auto_matched") -> UUID:
    name_id = uuid4()
    conn.execute(
        "INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status)"
        " VALUES (%s, %s, %s, %s)",
        (name_id, spelling, spelling.lower(), status),
    )
    return name_id


def _artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, %s, %s, %s, FALSE)",
        (artist_id, name, name, name.lower(), mbid, "musicbrainz" if mbid else "local"),
    )
    return artist_id


def _old_row(
    conn: Conn,
    name_id: UUID,
    target: str,
    *,
    tier: str = "normalization",
    score: float = 87.5,
    at: datetime = T0,
) -> UUID:
    """An artist row of ``matches``, as the old matcher wrote it before 1a."""
    match_id = uuid4()
    conn.execute(
        """INSERT INTO matches
               (id, artist_id, target_id, target_type, confidence_score, match_tier, created_at)
           VALUES (%s, %s, %s, 'artist', %s, %s, %s)""",
        (match_id, name_id, target, score, tier, at),
    )
    return match_id


def _copied_link(
    conn: Conn, name_id: UUID, artist_id: str, match_id: UUID, *, state: str = "active"
) -> None:
    """The link 0044 made from the old row *match_id*: its evidence v0 holds the row; its
    strength is the score, 100 for a manual-tier row (1a's S2)."""
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, state, origin, strength, evidence, created_at)
           SELECT %s, %s, %s, 'legacy',
                  CASE WHEN m.match_tier = 'manual' THEN 100
                       ELSE round(m.confidence_score::numeric) END,
                  jsonb_build_object('version', 0, 'legacy', jsonb_build_object(
                      'match_id', m.id, 'target_id', m.target_id, 'tier', m.match_tier,
                      'score', m.confidence_score, 'created_at', m.created_at)),
                  m.created_at
             FROM matches m WHERE m.id = %s""",
        (name_id, artist_id, state, match_id),
    )


def _new_link(conn: Conn, name_id: UUID, artist_id: str) -> None:
    """A link the matcher or Resolve wrote since 1a: no old row behind it."""
    conn.execute(
        """INSERT INTO log_name_links
               (broadcast_artist_id, artist_id, state, origin, strength, evidence)
           VALUES (%s, %s, 'active', 'manual', 100, %s)""",
        (name_id, artist_id, json.dumps({"version": 0, "tier": "manual", "score": 1.0})),
    )


def _song_row(conn: Conn, name_id: UUID, title: str) -> UUID:
    """A line of the name and its song match (no file: a review guess)."""
    line_id = uuid4()
    conn.execute(
        """INSERT INTO track_identities
               (id, broadcast_artist_id, original_title, normalized_title, normalized_signature,
                match_status)
           VALUES (%s, %s, %s, %s, %s, 'needs_review')""",
        (line_id, name_id, title, title.lower(), f"{name_id}:{title.lower()}"),
    )
    match_id = uuid4()
    conn.execute(
        """INSERT INTO matches (id, identity_id, confidence_score, match_tier, created_at)
           VALUES (%s, %s, 0.62, 'local_file_fuzzy', %s)""",
        (match_id, line_id, T0),
    )
    return match_id


# --- reads ---------------------------------------------------------------------------------------


def _columns(conn: Conn, table: str) -> dict[str, str]:
    rows = conn.execute(
        """SELECT column_name, is_nullable FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s""",
        (table,),
    ).fetchall()
    return {r["column_name"]: r["is_nullable"] for r in rows}


def _constraints(conn: Conn) -> set[str]:
    rows = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'public.matches'::regclass"
    ).fetchall()
    return {r["conname"] for r in rows}


def _indexes(conn: Conn) -> set[str]:
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = 'matches'"
    ).fetchall()
    return {r["indexname"] for r in rows}


def _table_exists(conn: Conn, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()
    return row is not None and row["t"] is not None


def _applied(conn: Conn, version: str) -> bool:
    row = conn.execute("SELECT 1 FROM schema_migrations WHERE version = %s", (version,)).fetchone()
    return row is not None


def _artist_rows(conn: Conn) -> dict[UUID, dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(ROW_COLUMNS)}, created_at FROM matches WHERE artist_id IS NOT NULL"
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _song_rows(conn: Conn) -> dict[UUID, dict[str, Any]]:
    """The song rows, every column a song row has with or without 0046."""
    rows = conn.execute(
        f"SELECT {', '.join(SONG_COLUMNS)} FROM matches WHERE identity_id IS NOT NULL"
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


# What 0046 drops of the artist column, and its rollback puts back.
ARTIST_CONSTRAINTS = {
    "xor_match_target",
    "matches_artist_id_target_id_key",
    "matches_artist_id_fkey",
}
ARTIST_INDEX = "idx_matches_artist"


# --- S10, S11: the schema after 0046 -------------------------------------------------------------


@pytest.mark.integration
def test_the_schema_after_0046(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        matches = _columns(conn, "matches")
        names = _columns(conn, "broadcast_artists")
        constraints, indexes = _constraints(conn), _indexes(conn)
        stubs = _table_exists(conn, "link_copy_stubs")
        applied = _applied(conn, VERSION)

    assert "artist_id" not in matches
    assert matches["identity_id"] == "NO"  # a match is a song's
    assert constraints & ARTIST_CONSTRAINTS == set()
    assert ARTIST_INDEX not in indexes
    assert "idx_matches_identity" in indexes  # the song rows' own index stays
    assert not stubs
    assert {"artist_candidates", "resolved_artist_mbid"} & set(names) == set()
    assert applied


@pytest.mark.integration
def test_artist_rows_go_and_song_rows_stay(migrated_db: str) -> None:
    with before_the_drop(migrated_db) as conn:
        name = _name(conn, "METALLICA")
        artist = _artist(conn, "Metallica", "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab")
        old = _old_row(conn, name, "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab")
        _copied_link(conn, name, artist, old)
        song = _song_row(conn, name, "One")
        songs_before = _song_rows(conn)
        links_before = conn.execute("SELECT * FROM log_name_links").fetchall()

        _migrate(conn)

        songs_after = _song_rows(conn)
        links_after = conn.execute("SELECT * FROM log_name_links").fetchall()
        left = conn.execute("SELECT count(*) AS n FROM matches").fetchone()

    assert left is not None and left["n"] == 1  # the song row only
    assert songs_after == songs_before and song in songs_after
    assert links_after == links_before  # the links are not touched


@pytest.mark.integration
@pytest.mark.parametrize("on_dev", [False, True], ids=["fresh install", "dev database"])
def test_the_drops_hold_on_either_database(migrated_db: str, on_dev: bool) -> None:
    """``resolved_artist_mbid`` exists only on the dev database: 0046 runs with it or
    without it, and leaves neither C34 column."""
    with before_the_drop(migrated_db) as conn:
        if on_dev:
            conn.execute("ALTER TABLE broadcast_artists ADD COLUMN resolved_artist_mbid TEXT")
        assert "artist_candidates" in _columns(conn, "broadcast_artists")

        _migrate(conn)

        names = _columns(conn, "broadcast_artists")

    assert {"artist_candidates", "resolved_artist_mbid"} & set(names) == set()


def _demoted_by_0045(conn: Conn, name_id: UUID) -> None:
    """0045 turned the auto-matched name's links (stored as suggestions) into suggestions,
    keeping each link and the name's status, before and after, in ``link_demotion_backup``."""
    conn.execute(
        """INSERT INTO link_demotion_backup (kind, id, why, before, after)
           SELECT 'link', l.id, 'demoted', to_jsonb(l) || '{"state": "active"}', to_jsonb(l)
             FROM log_name_links l WHERE l.broadcast_artist_id = %s""",
        (name_id,),
    )
    before = {"match_status": "auto_matched", "reason_code": None, "reason_detail": None}
    after = {
        "match_status": "needs_review",
        "reason_code": "LINK_SUGGESTED",
        "reason_detail": "1 suggested artist, best 62% (M22)",
    }
    conn.execute(
        """UPDATE broadcast_artists
              SET match_status = %s, reason_code = %s, reason_detail = %s
            WHERE id = %s""",
        (after["match_status"], after["reason_code"], after["reason_detail"], name_id),
    )
    conn.execute(
        """INSERT INTO link_demotion_backup (kind, id, why, before, after)
           VALUES ('name', %s, 'status', %s, %s)""",
        (name_id, json.dumps(before), json.dumps(after)),
    )


def _deleted_by_0045(conn: Conn, name_id: UUID, why: str = "removed") -> None:
    """0045 deleted the name's links (scored under 56, or to a placeholder), keeping each whole
    in ``link_demotion_backup``, with its v0 copy when it has one."""
    conn.execute(
        """INSERT INTO link_demotion_backup (kind, id, why, before, after)
           SELECT 'link', l.id, %s, to_jsonb(l), NULL
             FROM log_name_links l WHERE l.broadcast_artist_id = %s""",
        (why, name_id),
    )
    conn.execute("DELETE FROM log_name_links WHERE broadcast_artist_id = %s", (name_id,))


@pytest.mark.integration
def test_rollback_restores_rows(migrated_db: str) -> None:
    """One artist row back for each v0 copy of one, as the row was (its target too, not the
    artist's current key): each link's, in any state, and each link 0045 deleted, from
    ``link_demotion_backup``; none for a link written since 1a, kept or deleted by 0045; the
    duplicate 0044 collapsed stays gone; ``resolved_artist_mbid`` stays gone."""
    with before_the_drop(migrated_db) as conn:
        conn.execute("ALTER TABLE broadcast_artists ADD COLUMN resolved_artist_mbid TEXT")  # dev
        metallica = _artist(conn, "Metallica", "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab")
        local = _artist(conn, "Van Halen")
        archies = _artist(conn, "Archies", "0b0bb0d2-6bae-4c8e-8c58-9f38f5fe7a6e")
        low = _artist(conn, "Low Band")
        various = _artist(conn, "Various Artists", "89ad4ac3-39f7-470e-963a-56509c546377")
        active, rejected, demoted, removed, fresh, placeholder = (
            _name(conn, "METALLICA"),
            _name(conn, "MEGADETH", "manual_rejected"),
            _name(conn, "ARCHIE"),
            _name(conn, "LOW BAND", "needs_review"),
            _name(conn, "VAN HALEN", "manual_matched"),
            _name(conn, "VARIOUS", "needs_review"),
        )
        links = [
            (active, metallica, "active", "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab", {"score": 100.0}),
            # by catalog id, though the artist has an MBID: the row's own target comes back
            (
                rejected,
                metallica,
                "rejected",
                metallica,
                {"tier": "manual", "score": 1.0, "at": T0 + HOUR},
            ),
            (
                demoted,
                archies,
                "suggested",
                "0b0bb0d2-6bae-4c8e-8c58-9f38f5fe7a6e",
                {"score": 62.5},
            ),
            (removed, low, "active", low, {"score": 52.0, "at": T0 + 2 * HOUR}),
        ]
        kept: list[UUID] = []
        for name_id, artist_id, state, target, row in links:
            kept.append(_old_row(conn, name_id, target, **row))
            _copied_link(conn, name_id, artist_id, kept[-1], state=state)
        _demoted_by_0045(conn, demoted)
        _deleted_by_0045(conn, removed)
        # A duplicate of the active pair, which 0044 collapsed into the link above.
        _old_row(conn, active, metallica, at=T0 + 3 * HOUR)
        _new_link(conn, fresh, local)
        _new_link(conn, placeholder, various)  # since 1a, then deleted by 0045 (C7): no copy
        _deleted_by_0045(conn, placeholder, why="placeholder")
        song = _song_row(conn, active, "One")
        rows_before = _artist_rows(conn)
        songs_before = _song_rows(conn)
        _migrate(conn)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (VERSION,))

        _roll_back(conn)

        matches = _columns(conn, "matches")
        constraints, indexes = _constraints(conn), _indexes(conn)
        stubs = conn.execute("SELECT count(*) AS n FROM link_copy_stubs").fetchone()
        candidates = conn.execute(
            "SELECT count(*) AS n FROM broadcast_artists WHERE artist_candidates IS NOT NULL"
        ).fetchone()
        names = _columns(conn, "broadcast_artists")
        rows_after = _artist_rows(conn)
        songs_after = _song_rows(conn)
        applied = _applied(conn, VERSION)

    assert matches.get("artist_id") == "YES" and matches["identity_id"] == "YES"
    assert constraints >= ARTIST_CONSTRAINTS
    assert ARTIST_INDEX in indexes
    assert stubs is not None and stubs["n"] == 0  # back, and empty
    assert candidates is not None and candidates["n"] == 0  # back, and empty: never written
    assert "resolved_artist_mbid" not in names  # read by nothing: not put back
    assert rows_after == {match_id: rows_before[match_id] for match_id in kept}
    assert songs_after == songs_before and song in songs_after
    assert not applied


@pytest.mark.integration
def test_rollbacks_run_newest_first(migrated_db: str) -> None:
    """After 0046, its rollback, then 0045's, then 0044's, in one transaction: each finds what it
    needs (0044's reads the artist column and ``link_copy_stubs``), and the database is as
    before 0044. A demoted legacy link's old row is back once, as it was, under its name's old
    status; a link written since 1a is a row 0044's rollback made."""
    with before_the_drop(migrated_db) as conn:
        resolved = _name(conn, "METALLICA", "manual_matched")
        metallica = _artist(conn, "Metallica", "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab")
        _new_link(conn, resolved, metallica)
        demoted = _name(conn, "ARCHIE")
        archies = _artist(conn, "Archies", "0b0bb0d2-6bae-4c8e-8c58-9f38f5fe7a6e")
        old = _old_row(conn, demoted, "0b0bb0d2-6bae-4c8e-8c58-9f38f5fe7a6e", score=62.5)
        _copied_link(conn, demoted, archies, old, state="suggested")
        _demoted_by_0045(conn, demoted)
        old_rows = _artist_rows(conn)  # the demoted name's only: the resolved one's link has none
        _migrate(conn)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (VERSION,))

        for script in (ROLLBACK, ROLLBACK_0045, ROLLBACK_0044):
            conn.execute(script.read_text(encoding="utf-8"))

        applied = [
            v for v in (VERSION, "0045_link_demotion", "0044_log_name_links") if _applied(conn, v)
        ]
        tables = [
            t
            for t in ("log_name_links", "link_copy_stubs", "link_demotion_backup")
            if _table_exists(conn, t)
        ]
        made = conn.execute(
            "SELECT target_id, match_tier FROM matches WHERE artist_id = %s", (resolved,)
        ).fetchall()
        back = {k: v for k, v in _artist_rows(conn).items() if v["artist_id"] == demoted}
        status = conn.execute(
            "SELECT match_status, reason_code FROM broadcast_artists WHERE id = %s", (demoted,)
        ).fetchone()

    assert applied == []
    assert tables == []
    assert [(r["target_id"], r["match_tier"]) for r in made] == [
        ("65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab", "manual")
    ]
    assert back == {old: old_rows[old]}
    assert status is not None and (status["match_status"], status["reason_code"]) == (
        "auto_matched",
        None,
    )


def test_lock_timeout_set_first() -> None:
    """0a's rule for a migration that takes locks: ``lock_timeout`` before anything else."""
    statements = [
        s.strip()
        for s in re.sub(r"--[^\n]*", "", MIGRATION.read_text(encoding="utf-8")).split(";")
        if s.strip()
    ]
    assert statements[0].startswith("SET LOCAL lock_timeout")


# --- the domain and the ports --------------------------------------------------------------------


def test_a_match_is_a_songs_match() -> None:
    fields = {f.name: f for f in dataclasses.fields(Match)}

    assert "artist_id" not in fields
    assert fields["identity_id"].default is dataclasses.MISSING
    match = Match(
        id=uuid4(), confidence_score=0.9, match_tier=MatchTier.LOCAL_FILE_FUZZY, identity_id=uuid4()
    )
    assert match.identity_id is not None
    with pytest.raises(TypeError):
        Match(  # type: ignore[call-arg]
            id=uuid4(), confidence_score=0.9, match_tier=MatchTier.LOCAL_FILE_FUZZY
        )


def test_get_by_artist_left_the_match_ports() -> None:
    for repository in (MatchRepository, PgMatchRepository, FakeMatchRepository):
        assert not hasattr(repository, "get_by_artist"), repository.__name__
