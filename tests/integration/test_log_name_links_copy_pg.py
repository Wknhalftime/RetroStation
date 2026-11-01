"""Migration 0044 copies the artist rows of ``matches`` into ``log_name_links`` (artist model
phase 1a, SPEC S2, S3, S13, S17; E6, E7, E8; C30, C45, C50).

S2: every artist row of ``matches`` becomes one link per (log name, artist). The row's
``target_id`` resolves to ``artists.id`` by catalog id, else by MBID. The link is ``active``,
or ``rejected`` on a ``manual_rejected`` or ``auto_rejected`` name; its origin is ``manual``
for a manual-tier row on a ``manual_matched`` name, else ``legacy``; ``credit_part`` 0, role
``main``; its strength is the score (a manual-tier row is 100); its evidence is v0,
``{version: 0, legacy: {match_id, target_id, tier, score, created_at}}``; ``created_at`` is
the row's. A pair held by several rows collapses to one: a manual one first, then the earliest.

S3: a target MBID no catalog artist holds gets one stub artist: a new UUID id, the MBID as
``mbid``, ``name`` and ``sort_name``, ``normalized_name`` NULL, origin ``musicbrainz``,
``needs_enhancement`` true; its id goes into ``link_copy_stubs`` for the rollback.

S13: straight after the copy, ``scripts/check_log_name_links_copy.py`` maps every old row to
exactly one link (its own, or the one its duplicate collapsed into), and every link to a row.

S17: ``backend/db/rollback_0044_log_name_links.sql`` drops ``log_name_links``, deletes the
stubs nothing else references (a work), drops ``link_copy_stubs`` and deletes 0044's
``schema_migrations`` row. The old rows stay in ``matches`` throughout (M35).

Each test runs on the database as it was before 0044: the rollback script undoes 0044 inside
one transaction, the test seeds the old rows and runs the migration's SQL, and the
transaction is rolled back at the end, so the test database keeps 0044.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

Conn = psycopg.Connection[Any]

ROOT = Path(__file__).resolve().parents[2]
VERSION = "0044_log_name_links"
MIGRATION = ROOT / "backend" / "db" / "migrations" / f"{VERSION}.sql"
ROLLBACK = ROOT / "backend" / "db" / f"rollback_{VERSION}.sql"

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
METALLICA = "65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab"
NIRVANA = "5b11f4ce-a62d-471e-81fc-a69a8278c7da"
ARCHITECTS = "0b0bb0d2-6bae-4c8e-8c58-9f38f5fe7a6e"
UNKNOWN = "00000000-0000-4000-8000-0000000000aa"
UNKNOWN_2 = "00000000-0000-4000-8000-0000000000bb"
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"


@contextmanager
def before_the_copy(url: str) -> Iterator[Conn]:
    """The test database without 0044, in one transaction that is rolled back at the end."""
    conn = psycopg.connect(url, row_factory=dict_row)
    try:
        conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _migrate(conn: Conn) -> None:
    conn.execute(MIGRATION.read_text(encoding="utf-8"))


def _check_script() -> ModuleType:
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("check_log_name_links_copy")


def _name(conn: Conn, name: str, status: str = "auto_matched") -> UUID:
    name_id = uuid4()
    conn.execute(
        "INSERT INTO broadcast_artists (id, original_name, normalized_name, match_status)"
        " VALUES (%s, %s, %s, %s)",
        (name_id, name, name.lower(), status),
    )
    return name_id


def _local(conn: Conn, name: str) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, origin, needs_enhancement)"
        " VALUES (%s, %s, %s, %s, 'local', FALSE)",
        (artist_id, name, name, name.lower()),
    )
    return artist_id


def _musicbrainz(conn: Conn, name: str, mbid: str) -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin,"
        " needs_enhancement) VALUES (%s, %s, %s, %s, %s, 'musicbrainz', FALSE)",
        (artist_id, name, name, name.lower(), mbid),
    )
    return artist_id


def _artist_row(
    conn: Conn,
    name_id: UUID,
    target: str,
    *,
    tier: str = "normalization",
    score: float = 100.0,
    at: datetime = T0,
) -> UUID:
    """An artist row of ``matches``, as written before 1a."""
    match_id = uuid4()
    conn.execute(
        """INSERT INTO matches
               (id, artist_id, target_id, target_type, confidence_score, match_tier, created_at)
           VALUES (%s, %s, %s, 'artist', %s, %s, %s)""",
        (match_id, name_id, target, score, tier, at),
    )
    return match_id


def _links(conn: Conn) -> dict[UUID, list[dict[str, Any]]]:
    rows = conn.execute("SELECT * FROM log_name_links ORDER BY created_at, artist_id").fetchall()
    by_name: dict[UUID, list[dict[str, Any]]] = {}
    for row in rows:
        by_name.setdefault(row["broadcast_artist_id"], []).append(dict(row))
    return by_name


def _only_link(conn: Conn, name_id: UUID) -> dict[str, Any]:
    [link] = _links(conn)[name_id]
    return link


def _artist_ids(conn: Conn) -> set[str]:
    return {row["id"] for row in conn.execute("SELECT id FROM artists").fetchall()}


def _table_exists(conn: Conn, table: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()
    return row is not None and row["t"] is not None


def _assert_legacy_evidence(
    link: dict[str, Any], match_id: UUID, target: str, tier: str, score: float, at: datetime
) -> None:
    evidence = link["evidence"]
    assert evidence["version"] == 0
    legacy = evidence["legacy"]
    assert set(legacy) == {"match_id", "target_id", "tier", "score", "created_at"}
    assert legacy["match_id"] == str(match_id)
    assert legacy["target_id"] == target
    assert legacy["tier"] == tier
    assert legacy["score"] == pytest.approx(score, abs=1e-4)
    assert datetime.fromisoformat(legacy["created_at"]) == at


# --- S2: the four target forms -------------------------------------------------------------------


def test_each_target_form_copies(migrated_db: str) -> None:
    with before_the_copy(migrated_db) as conn:
        local = _local(conn, "Van Halen")  # a local-only artist, by catalog id
        gained = _local(conn, "Nirvana")  # by catalog id, given an MBID since
        conn.execute(
            "UPDATE artists SET mbid = %s, origin = 'musicbrainz' WHERE id = %s", (NIRVANA, gained)
        )
        held = _musicbrainz(conn, "Metallica", METALLICA)  # by MBID
        fuzzy = _musicbrainz(conn, "Architects", ARCHITECTS)

        by_local = _name(conn, "VAN HALEN")
        by_gained = _name(conn, "NIRVANA", "manual_matched")
        by_mbid = _name(conn, "METALLICA")
        by_rule = _name(conn, "RULE BAND")  # a mapping rule writes tier manual too
        by_fuzzy = _name(conn, "ARCHIES")
        by_half = _name(conn, "ARCHITECTS UK")
        rows = {
            by_local: (_artist_row(conn, by_local, local, at=T0), local, "normalization", 100.0),
            by_gained: (
                _artist_row(conn, by_gained, gained, tier="manual", score=1.0, at=T0 + HOUR),
                gained,
                "manual",
                1.0,
            ),
            by_mbid: (
                _artist_row(conn, by_mbid, METALLICA, tier="musicbrainz_api", at=T0 + 2 * HOUR),
                METALLICA,
                "musicbrainz_api",
                100.0,
            ),
            by_rule: (
                _artist_row(conn, by_rule, UNKNOWN, tier="manual", at=T0 + 3 * HOUR),
                UNKNOWN,
                "manual",
                100.0,
            ),
            by_fuzzy: (
                _artist_row(conn, by_fuzzy, ARCHITECTS, score=72.4, at=T0 + 4 * HOUR),
                ARCHITECTS,
                "normalization",
                72.4,
            ),
            by_half: (
                _artist_row(conn, by_half, ARCHITECTS, score=62.5, at=T0 + 5 * HOUR),
                ARCHITECTS,
                "normalization",
                62.5,
            ),
        }
        before = _artist_ids(conn)

        _migrate(conn)

        links = {name_id: _only_link(conn, name_id) for name_id in rows}
        [stub] = _artist_ids(conn) - before

    expected = {
        by_local: (local, None, "legacy", 100, T0),
        by_gained: (gained, NIRVANA, "manual", 100, T0 + HOUR),
        by_mbid: (held, METALLICA, "legacy", 100, T0 + 2 * HOUR),
        by_rule: (stub, UNKNOWN, "legacy", 100, T0 + 3 * HOUR),
        by_fuzzy: (fuzzy, ARCHITECTS, "legacy", 72, T0 + 4 * HOUR),
        by_half: (fuzzy, ARCHITECTS, "legacy", 63, T0 + 5 * HOUR),  # half up
    }
    for name_id, (artist_id, mbid, origin, strength, created_at) in expected.items():
        link = links[name_id]
        assert (link["artist_id"], link["mbid"], link["origin"]) == (artist_id, mbid, origin)
        assert (link["state"], link["strength"], link["created_at"]) == (
            "active",
            strength,
            created_at,
        )
        assert (link["credit_part"], link["role"], link["join_after"]) == (0, "main", None)
        match_id, target, tier, score = rows[name_id]
        _assert_legacy_evidence(link, match_id, target, tier, score, created_at)


@pytest.mark.parametrize("status", ["manual_rejected", "auto_rejected"])
def test_a_rejected_names_links_copy_as_rejected(migrated_db: str, status: str) -> None:
    with before_the_copy(migrated_db) as conn:
        artist_id = _musicbrainz(conn, "Metallica", METALLICA)
        name_id = _name(conn, "METALLICA", status)
        _artist_row(conn, name_id, METALLICA, tier="manual", score=1.0)

        _migrate(conn)

        link = _only_link(conn, name_id)

    assert (link["artist_id"], link["state"], link["origin"]) == (artist_id, "rejected", "legacy")


def test_duplicate_pairs_collapse_manual_first_then_earliest(migrated_db: str) -> None:
    """JIMI HENDRIX on the dev database: three rows, two of them for one artist (by catalog id
    and by MBID). A manual row wins its pair; with none, the earliest does."""
    with before_the_copy(migrated_db) as conn:
        jimi = _musicbrainz(conn, "Jimi Hendrix", NIRVANA)
        other = _musicbrainz(conn, "Metallica", METALLICA)
        manual_name = _name(conn, "JIMI HENDRIX", "manual_matched")
        _artist_row(conn, manual_name, jimi, at=T0)  # automatic, earlier
        manual = _artist_row(conn, manual_name, NIRVANA, tier="manual", score=1.0, at=T0 + HOUR)
        _artist_row(conn, manual_name, METALLICA, score=80.0, at=T0 + 2 * HOUR)
        automatic_name = _name(conn, "JIMI HENDRIX EXPERIENCE")
        later = _artist_row(conn, automatic_name, NIRVANA, score=90.0, at=T0 + HOUR)
        earliest = _artist_row(conn, automatic_name, jimi, score=70.0, at=T0)

        _migrate(conn)

        links = _links(conn)

    by_artist = {link["artist_id"]: link for link in links[manual_name]}
    assert set(by_artist) == {jimi, other}
    kept = by_artist[jimi]
    assert (kept["origin"], kept["strength"], kept["created_at"]) == ("manual", 100, T0 + HOUR)
    assert kept["evidence"]["legacy"]["match_id"] == str(manual)
    [collapsed] = links[automatic_name]
    assert (collapsed["artist_id"], collapsed["origin"], collapsed["strength"]) == (
        jimi,
        "legacy",
        70,
    )
    assert collapsed["evidence"]["legacy"]["match_id"] == str(earliest)
    assert collapsed["evidence"]["legacy"]["match_id"] != str(later)


# --- S3: stubs -----------------------------------------------------------------------------------


def test_unknown_mbids_get_stub_artists(migrated_db: str) -> None:
    with before_the_copy(migrated_db) as conn:
        known = _musicbrainz(conn, "Metallica", METALLICA)
        first, second, third = (_name(conn, f"NAME {n}") for n in range(3))
        _artist_row(conn, first, UNKNOWN)
        _artist_row(conn, second, UNKNOWN, tier="manual")  # one stub per MBID
        _artist_row(conn, third, UNKNOWN_2)
        _artist_row(conn, _name(conn, "METALLICA"), METALLICA)
        before = _artist_ids(conn)

        _migrate(conn)

        stub_ids = _artist_ids(conn) - before
        stubs = {
            row["mbid"]: dict(row)
            for row in conn.execute(
                "SELECT * FROM artists WHERE id = ANY(%s)", (list(stub_ids),)
            ).fetchall()
        }
        recorded = {
            row["artist_id"] for row in conn.execute("SELECT * FROM link_copy_stubs").fetchall()
        }
        linked = {name_id: _only_link(conn, name_id)["artist_id"] for name_id in (first, second)}

    assert set(stubs) == {UNKNOWN, UNKNOWN_2}
    for mbid, stub in stubs.items():
        UUID(stub["id"])  # gen_random_uuid()::text
        assert stub["id"] != mbid
        assert (stub["name"], stub["sort_name"], stub["normalized_name"]) == (mbid, mbid, None)
        assert (stub["origin"], stub["needs_enhancement"]) == ("musicbrainz", True)
    assert recorded == stub_ids
    assert known not in recorded
    assert linked[first] == linked[second] == stubs[UNKNOWN]["id"]


# --- S13: old and new match one for one ----------------------------------------------------------


def test_old_and_new_match_one_for_one(migrated_db: str) -> None:
    check = _check_script().check
    with before_the_copy(migrated_db) as conn:
        jimi = _musicbrainz(conn, "Jimi Hendrix", NIRVANA)
        local = _local(conn, "Van Halen")
        hendrix = _name(conn, "JIMI HENDRIX", "manual_matched")
        rows = [
            _artist_row(conn, hendrix, jimi, at=T0),
            _artist_row(conn, hendrix, NIRVANA, tier="manual", score=1.0, at=T0 + HOUR),
            _artist_row(conn, _name(conn, "VAN HALEN"), local),
            _artist_row(conn, _name(conn, "RULE BAND"), UNKNOWN, tier="manual"),
            _artist_row(conn, _name(conn, "SPOT BREAK", "manual_rejected"), local),
        ]

        _migrate(conn)
        clean = check(conn)

        dropped = conn.execute(
            "DELETE FROM log_name_links WHERE broadcast_artist_id = %s RETURNING id", (hendrix,)
        ).fetchone()
        stray_name = _name(conn, "NOT IN MATCHES")
        conn.execute(
            """INSERT INTO log_name_links
                   (broadcast_artist_id, artist_id, state, origin, strength, evidence)
               VALUES (%s, %s, 'active', 'automatic', 90, '{"version": 0}')""",
            (stray_name, local),
        )
        stray = conn.execute(
            "SELECT id FROM log_name_links WHERE broadcast_artist_id = %s", (stray_name,)
        ).fetchone()
        broken = check(conn)

    assert dropped is not None and stray is not None
    assert clean.ok
    assert (clean.old_rows, clean.links, clean.collapsed) == (5, 4, 1)
    assert (clean.unmapped_rows, clean.links_without_rows) == ((), ())
    assert not broken.ok
    assert sorted(broken.unmapped_rows) == sorted(str(r) for r in rows[:2])
    assert broken.links_without_rows == (str(stray["id"]),)


# --- S17: the rollback ---------------------------------------------------------------------------


def test_rollback(migrated_db: str) -> None:
    with before_the_copy(migrated_db) as conn:
        known = _musicbrainz(conn, "Metallica", METALLICA)
        _artist_row(conn, _name(conn, "METALLICA"), METALLICA)
        _artist_row(conn, _name(conn, "RULE BAND"), UNKNOWN)
        _artist_row(conn, _name(conn, "OTHER RULE BAND"), UNKNOWN_2)
        before = _artist_ids(conn)
        old_rows = conn.execute(
            "SELECT count(*) AS n FROM matches WHERE artist_id IS NOT NULL"
        ).fetchone()
        _migrate(conn)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (VERSION,))
        kept_stub = conn.execute("SELECT id FROM artists WHERE mbid = %s", (UNKNOWN_2,)).fetchone()
        assert kept_stub is not None
        conn.execute(
            "INSERT INTO works (id, title, artist_id, origin) VALUES (%s, 'Song', %s, 'local')",
            (str(uuid4()), kept_stub["id"]),
        )

        conn.execute(ROLLBACK.read_text(encoding="utf-8"))

        after = _artist_ids(conn)
        rows_after = conn.execute(
            "SELECT count(*) AS n FROM matches WHERE artist_id IS NOT NULL"
        ).fetchone()
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = %s", (VERSION,)
        ).fetchone()
        tables = (_table_exists(conn, "log_name_links"), _table_exists(conn, "link_copy_stubs"))

    assert tables == (False, False)
    assert applied is None
    assert after == before | {kept_stub["id"]}  # the stub a work references stays
    assert known in after
    assert old_rows is not None and rows_after is not None
    assert rows_after["n"] == old_rows["n"] == 3


# --- E6, E7, E8 ----------------------------------------------------------------------------------


def test_copy_is_one_transaction(migrated_db: str) -> None:
    """E6: the runner runs each migration in one transaction, so a copy that fails part way
    (here a score the strength CHECK refuses, after the stubs were made) leaves nothing."""
    with before_the_copy(migrated_db) as conn:
        _artist_row(conn, _name(conn, "RULE BAND"), UNKNOWN)
        _artist_row(conn, _name(conn, "BAD ROW"), _local(conn, "Bad Row"), score=150.0)
        before = _artist_ids(conn)

        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            _migrate(conn)

        tables = (_table_exists(conn, "log_name_links"), _table_exists(conn, "link_copy_stubs"))
        after = _artist_ids(conn)

    assert tables == (False, False)
    assert after == before


def test_empty_matches(migrated_db: str) -> None:
    """E7: a fresh install, or song rows only: an empty table and no stubs."""
    with before_the_copy(migrated_db) as conn:
        name_id = _name(conn, "METALLICA", "pending")
        identity = uuid4()
        conn.execute(
            """INSERT INTO track_identities
                   (id, broadcast_artist_id, original_title, normalized_title,
                    normalized_signature)
               VALUES (%s, %s, 'One', 'one', %s)""",
            (identity, name_id, f"metallica:one:{identity.hex}"),
        )
        conn.execute(
            "INSERT INTO matches (id, identity_id, confidence_score, match_tier)"
            " VALUES (%s, %s, 90.0, 'local_file_fuzzy')",
            (uuid4(), identity),
        )
        before = _artist_ids(conn)

        _migrate(conn)

        links = conn.execute("SELECT count(*) AS n FROM log_name_links").fetchone()
        stubs = conn.execute("SELECT count(*) AS n FROM link_copy_stubs").fetchone()
        after = _artist_ids(conn)

    assert links is not None and links["n"] == 0
    assert stubs is not None and stubs["n"] == 0
    assert after == before


def test_placeholder_links_copy(migrated_db: str) -> None:
    """E8: three log names linked to Various Artists copy as they are; 1c removes them (C7)."""
    with before_the_copy(migrated_db) as conn:
        various = _musicbrainz(conn, "Various Artists", VARIOUS_ARTISTS)
        names = [_name(conn, f"VARIOUS {n}") for n in range(3)]
        for name_id in names:
            _artist_row(conn, name_id, VARIOUS_ARTISTS, tier="musicbrainz_api")

        _migrate(conn)

        links = [_only_link(conn, name_id) for name_id in names]

    assert [(link["artist_id"], link["state"]) for link in links] == [(various, "active")] * 3
