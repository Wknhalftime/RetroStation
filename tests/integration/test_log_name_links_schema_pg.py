"""Migration 0044 creates ``log_name_links`` (artist model phase 1a, SPEC S1; C30, AUD-R035).

One row per (log name, artist), unique in every state, so a rejected pair stays remembered
(C19). ``broadcast_artist_id`` (UUID) and ``artist_id`` (TEXT) are foreign keys with no
cascade; ``mbid`` is the artist's MBID copied alongside. ``state``, ``origin`` and ``role``
hold known words only, ``strength`` is 0 to 100, ``evidence`` is a JSON object with a
``version``, and ``credit_part`` is not negative. ``artist_id`` is indexed. The migration sets
``SET LOCAL lock_timeout`` and analyzes what it fills (the migration lint, AUD-R044).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.test_migration_lint import lint

Conn = psycopg.Connection[Any]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "db"
    / "migrations"
    / "0044_log_name_links.sql"
)

COLUMNS = {
    "id": ("uuid", "NO"),
    "broadcast_artist_id": ("uuid", "NO"),
    "artist_id": ("text", "NO"),
    "mbid": ("text", "YES"),
    "state": ("text", "NO"),
    "origin": ("text", "NO"),
    "strength": ("smallint", "NO"),
    "evidence": ("jsonb", "NO"),
    "credit_part": ("smallint", "NO"),
    "role": ("text", "NO"),
    "join_after": ("text", "YES"),
    "created_at": ("timestamp with time zone", "NO"),
    "updated_at": ("timestamp with time zone", "NO"),
}


def _connect(url: str) -> Conn:
    return psycopg.connect(url, row_factory=dict_row)


def _name(conn: Conn, name: str = "METALLICA") -> UUID:
    name_id = uuid4()
    conn.execute(
        "INSERT INTO broadcast_artists (id, original_name, normalized_name) VALUES (%s, %s, %s)",
        (name_id, name, f"{name.lower()} {name_id.hex[:6]}"),
    )
    return name_id


def _artist(conn: Conn, name: str = "Metallica") -> str:
    artist_id = str(uuid4())
    conn.execute(
        "INSERT INTO artists (id, name, sort_name, normalized_name, origin, needs_enhancement)"
        " VALUES (%s, %s, %s, %s, 'local', FALSE)",
        (artist_id, name, name, f"{name.lower()} {artist_id[:6]}"),
    )
    return artist_id


def _insert(conn: Conn, name_id: UUID, artist_id: str, **columns: object) -> None:
    row: dict[str, object] = {
        "broadcast_artist_id": name_id,
        "artist_id": artist_id,
        "state": "active",
        "origin": "automatic",
        "strength": 90,
        "evidence": json.dumps({"version": 0}),
    }
    row.update(columns)
    names = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    conn.execute(f"INSERT INTO log_name_links ({names}) VALUES ({marks})", tuple(row.values()))


def test_table_constraints_and_indexes(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0044_log_name_links'"
        ).fetchone()
        columns = conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default"
            " FROM information_schema.columns WHERE table_name = 'log_name_links'"
        ).fetchall()
        foreign_keys = conn.execute(
            """SELECT a.attname AS column, c.confrelid::regclass::text AS target,
                      c.confdeltype, c.confupdtype
                 FROM pg_constraint c
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                WHERE c.conrelid = 'log_name_links'::regclass AND c.contype = 'f'"""
        ).fetchall()
        unique_keys = conn.execute(
            """SELECT array_agg(a.attname::text ORDER BY a.attname) AS columns
                 FROM pg_constraint c
                 JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
                WHERE c.conrelid = 'log_name_links'::regclass AND c.contype = 'u'
                GROUP BY c.oid"""
        ).fetchall()
        indexes = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'log_name_links'"
        ).fetchall()

    assert applied is not None
    assert {c["column_name"]: (c["data_type"], c["is_nullable"]) for c in columns} == COLUMNS
    defaults = {c["column_name"]: c["column_default"] for c in columns}
    assert defaults["credit_part"] == "0"
    assert str(defaults["role"]).startswith("'main'")
    assert {(f["column"], f["target"]) for f in foreign_keys} == {
        ("broadcast_artist_id", "broadcast_artists"),
        ("artist_id", "artists"),
    }
    no_cascade = {"a", "r"}  # NO ACTION or RESTRICT
    assert all({f["confdeltype"], f["confupdtype"]} <= no_cascade for f in foreign_keys)
    assert [sorted(u["columns"]) for u in unique_keys] == [["artist_id", "broadcast_artist_id"]]
    assert any("(artist_id)" in i["indexdef"] and "UNIQUE" not in i["indexdef"] for i in indexes), (
        indexes
    )


def test_one_row_per_name_and_artist_in_every_state(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        name_id, artist_id = _name(conn), _artist(conn)
        _insert(conn, name_id, artist_id, state="rejected", origin="manual")
        _insert(conn, name_id, _artist(conn, "Megadeth"), state="suggested")
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            _insert(conn, name_id, artist_id, state="active")
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            _insert(conn, name_id, artist_id, state="suggested")
        rows = conn.execute(
            "SELECT count(*) AS n FROM log_name_links WHERE broadcast_artist_id = %s", (name_id,)
        ).fetchone()

    assert rows is not None and rows["n"] == 2


@pytest.mark.parametrize(
    "columns",
    [
        {"state": "linked"},
        {"origin": "matcher"},
        {"role": "guest"},
        {"strength": -1},
        {"strength": 101},
        {"evidence": json.dumps({"tier": "manual"})},
        {"evidence": json.dumps([{"version": 0}])},
        {"credit_part": -1},
    ],
    ids=str,
)
def test_the_table_refuses_what_its_checks_rule_out(
    migrated_db: str, columns: dict[str, object]
) -> None:
    with _connect(migrated_db) as conn:
        name_id, artist_id = _name(conn), _artist(conn)
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            _insert(conn, name_id, artist_id, **columns)


@pytest.mark.parametrize(
    ("state", "origin", "role", "strength"),
    [
        ("active", "automatic", "main", 0),
        ("suggested", "one_time_pass", "featured", 100),
        ("rejected", "legacy", "main", 55),
        ("active", "manual", "featured", 100),
    ],
)
def test_the_table_accepts_every_known_word(
    migrated_db: str, state: str, origin: str, role: str, strength: int
) -> None:
    with _connect(migrated_db) as conn:
        name_id, artist_id = _name(conn), _artist(conn)
        _insert(
            conn,
            name_id,
            artist_id,
            state=state,
            origin=origin,
            role=role,
            strength=strength,
            credit_part=1,
            join_after=" & ",
            mbid="65f4f0c5-ef9e-490c-aee3-909e7ae6b2ab",
        )
        row = conn.execute(
            "SELECT * FROM log_name_links WHERE broadcast_artist_id = %s", (name_id,)
        ).fetchone()

    assert row is not None
    assert (row["state"], row["origin"], row["role"], row["strength"]) == (
        state,
        origin,
        role,
        strength,
    )
    assert row["created_at"] is not None and row["updated_at"] is not None
    assert isinstance(row["id"], UUID)


def test_foreign_keys_refuse_a_missing_target_and_never_cascade(migrated_db: str) -> None:
    with _connect(migrated_db) as conn:
        name_id, artist_id = _name(conn), _artist(conn)
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            _insert(conn, name_id, "no-such-artist")
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            _insert(conn, uuid4(), artist_id)
        _insert(conn, name_id, artist_id)
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute("DELETE FROM artists WHERE id = %s", (artist_id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute("DELETE FROM broadcast_artists WHERE id = %s", (name_id,))
        kept = conn.execute(
            "SELECT count(*) AS n FROM log_name_links WHERE artist_id = %s", (artist_id,)
        ).fetchone()

    assert kept is not None and kept["n"] == 1


def test_the_migration_sets_a_lock_timeout_and_analyzes_what_it_fills() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert lint(sql) == []
    assert "SET LOCAL lock_timeout" in sql
    assert "ANALYZE log_name_links" in sql
