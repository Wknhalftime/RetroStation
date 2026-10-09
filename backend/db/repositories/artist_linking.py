"""PostgreSQL adapter for the local-artist linker (AUD-R026; spec 2026-10-05 D15)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import psycopg

from backend.db.repositories.artists import artist_from_row
from backend.domain.catalog import (
    Artist,
    ArtistLinkCandidate,
    InvalidLinkDecisionError,
    LinkEvidence,
)
from backend.domain.enums import ArtistLinkOutcome
from backend.repositories.artist_linking import ArtistLinkingRepository

_DUE_SQL = """
SELECT a.*
  FROM artists a
 WHERE a.origin = 'local'
   AND a.mbid IS NULL
   AND (a.mb_lookup_at IS NULL
        OR EXISTS (SELECT 1
                     FROM library_files lf
                    WHERE lf.normalized_artist_name = a.normalized_name
                      AND lf.file_status = 'present'
                      AND lf.indexed_at > a.mb_lookup_at))
 ORDER BY a.normalized_name, a.id
"""

_TAGS_SQL = """
SELECT artist_mbid, count(*) AS files
  FROM library_files
 WHERE normalized_artist_name = %s
   AND file_status = 'present'
   AND artist_mbid IS NOT NULL
 GROUP BY artist_mbid
 ORDER BY count(*) DESC, artist_mbid
"""

# One statement. The NOT EXISTS means artists_mbid_key is never hit: a refused link is recorded
# as a duplicate, never retried as a UniqueViolation every run (AUD-R026; AUD-019's loop).
_LINK_SQL = """
UPDATE artists
   SET mbid = %(mbid)s,
       origin = 'musicbrainz',
       name = %(name)s,
       sort_name = %(sort_name)s,
       disambiguation = %(disambiguation)s,
       needs_enhancement = FALSE,
       enhanced_at = now(),
       mb_lookup_at = now(),
       mb_lookup_outcome = 'linked'
 WHERE id = %(id)s
   AND mbid IS NULL
   AND NOT EXISTS (SELECT 1 FROM artists other WHERE other.mbid = %(mbid)s)
"""

_OUTCOME_SQL = """
UPDATE artists
   SET mb_lookup_at = now(), mb_lookup_outcome = %s
 WHERE id = %s AND mbid IS NULL
"""

# The catalog name, plus the broadcast names matched to it: a manual match can carry a
# broadcast name that differs (dev DB: 5 of 598 matched rows on tag-linkable artists).
_LINKED_NAMES_SQL = """
SELECT a.normalized_name AS name
  FROM artists a
 WHERE a.mb_lookup_outcome = 'linked'
   AND a.mb_lookup_at > %(when)s
   AND a.normalized_name IS NOT NULL
UNION
SELECT ba.normalized_name AS name
  FROM artists a
  JOIN matches m
    ON m.target_type = 'artist' AND (m.target_id = a.id OR m.target_id = a.mbid)
  JOIN broadcast_artists ba ON ba.id = m.artist_id
 WHERE a.mb_lookup_outcome = 'linked'
   AND a.mb_lookup_at > %(when)s
"""


class PgArtistLinkingRepository(ArtistLinkingRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def list_due(self) -> list[Artist]:
        return [artist_from_row(row) for row in self._conn.execute(_DUE_SQL).fetchall()]

    def evidence(self, normalized_name: str) -> LinkEvidence:
        rows = self._conn.execute(_TAGS_SQL, (normalized_name,)).fetchall()
        return LinkEvidence(tag_counts=tuple((row["artist_mbid"], row["files"]) for row in rows))

    def link(self, artist_id: str, candidate: ArtistLinkCandidate) -> bool:
        cur = self._conn.execute(
            _LINK_SQL,
            {
                "id": artist_id,
                "mbid": candidate.mbid,
                "name": candidate.name,
                "sort_name": candidate.sort_name,
                "disambiguation": candidate.disambiguation,
            },
        )
        return cur.rowcount == 1

    def record_outcome(self, artist_id: str, outcome: ArtistLinkOutcome) -> None:
        if outcome == ArtistLinkOutcome.LINKED:
            raise InvalidLinkDecisionError("a link is written by link(), not record_outcome()")
        self._conn.execute(_OUTCOME_SQL, (outcome.value, artist_id))

    def normalized_names_linked_since(self, when: datetime) -> set[str]:
        rows = self._conn.execute(_LINKED_NAMES_SQL, {"when": when}).fetchall()
        return {row["name"] for row in rows}
