"""PostgreSQL adapter for log-name links, ``log_name_links`` (migration 0044; C30, AUD-R035).

The only module that writes the table (``tests/test_log_name_links_single_writer.py``).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.enums import CreditRole, LogNameLinkOrigin, LogNameLinkState
from backend.domain.matching import LogNameLink, LogNameLinkTargetMissingError, MatchingStorageError
from backend.repositories.log_name_links import LogNameLinkRepository

_COLUMNS = """id, broadcast_artist_id, artist_id, mbid, state, origin, strength, evidence,
              credit_part, role, join_after, created_at, updated_at"""

_UPSERT_SQL = f"""
INSERT INTO log_name_links
    (id, broadcast_artist_id, artist_id, mbid, state, origin, strength, evidence,
     credit_part, role, join_after, created_at, updated_at)
VALUES (%(id)s, %(name)s, %(artist)s, %(mbid)s, %(state)s, %(origin)s, %(strength)s,
        %(evidence)s, %(credit_part)s, %(role)s, %(join_after)s, %(created_at)s, now())
ON CONFLICT (broadcast_artist_id, artist_id) DO UPDATE SET
    mbid = EXCLUDED.mbid,
    state = EXCLUDED.state,
    origin = EXCLUDED.origin,
    strength = EXCLUDED.strength,
    evidence = EXCLUDED.evidence,
    credit_part = EXCLUDED.credit_part,
    role = EXCLUDED.role,
    join_after = EXCLUDED.join_after,
    updated_at = now()
RETURNING {_COLUMNS}
"""


def _link_from_row(row: dict[str, Any]) -> LogNameLink:
    return LogNameLink(
        id=row["id"],
        broadcast_artist_id=row["broadcast_artist_id"],
        artist_id=row["artist_id"],
        mbid=row["mbid"],
        state=LogNameLinkState(row["state"]),
        origin=LogNameLinkOrigin(row["origin"]),
        strength=row["strength"],
        evidence=row["evidence"],
        credit_part=row["credit_part"],
        role=CreditRole(row["role"]),
        join_after=row["join_after"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@translate_lost_connection(MatchingStorageError)
class PgLogNameLinkRepository(LogNameLinkRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def list_by_name(self, broadcast_artist_id: UUID) -> list[LogNameLink]:
        rows = self._conn.execute(
            f"""SELECT {_COLUMNS} FROM log_name_links
                 WHERE broadcast_artist_id = %s ORDER BY created_at, id""",
            (broadcast_artist_id,),
        ).fetchall()
        return [_link_from_row(row) for row in rows]

    def list_by_artist(self, artist_id: str) -> list[LogNameLink]:
        rows = self._conn.execute(
            f"""SELECT {_COLUMNS} FROM log_name_links
                 WHERE artist_id = %s ORDER BY created_at, id""",
            (artist_id,),
        ).fetchall()
        return [_link_from_row(row) for row in rows]

    def upsert_link(self, link: LogNameLink) -> LogNameLink:
        params = {
            "id": link.id,
            "name": link.broadcast_artist_id,
            "artist": link.artist_id,
            "mbid": link.mbid,
            "state": link.state.value,
            "origin": link.origin.value,
            "strength": link.strength,
            "evidence": Jsonb(dict(link.evidence)),
            "credit_part": link.credit_part,
            "role": link.role.value,
            "join_after": link.join_after,
            "created_at": link.created_at,
        }
        try:
            with self._conn.transaction():
                row = self._conn.execute(_UPSERT_SQL, params).fetchone()
        except psycopg.errors.ForeignKeyViolation as missing:
            raise LogNameLinkTargetMissingError(str(missing)) from missing
        if row is None:
            raise RuntimeError("log_name_links upsert returned no row")
        return _link_from_row(row)

    def set_link_state(
        self, broadcast_artist_id: UUID, artist_id: str, state: LogNameLinkState
    ) -> bool:
        cur = self._conn.execute(
            """UPDATE log_name_links SET state = %s, updated_at = now()
                WHERE broadcast_artist_id = %s AND artist_id = %s""",
            (state.value, broadcast_artist_id, artist_id),
        )
        return cur.rowcount == 1

    def delete_links_of_name(self, broadcast_artist_id: UUID) -> int:
        cur = self._conn.execute(
            "DELETE FROM log_name_links WHERE broadcast_artist_id = %s", (broadcast_artist_id,)
        )
        return cur.rowcount
