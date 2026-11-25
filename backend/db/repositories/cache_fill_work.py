"""The cache fill's work lists on PostgreSQL (phase 4.1 PR 2; SPEC S3; design C8, C18, C68;
AUD-R091).

A pair's plays are its lines' ``station_identity_plays`` summed, and its title is its most-played
line's original title (ties by the title). C8 counts acts as one key each: ``mbid:`` for any MBID
(a file's credited artists, or a linked catalog artist holding a real one), ``artist:`` for a
linked catalog artist with none, ``name:`` for an untagged file's normalized artist name; so an
act on both sides, or two log spellings linked to it, count once.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

import psycopg

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.domain.cache_fill import (
    ManualTitle,
    NamePair,
    QueuedPair,
    UnlinkedRecording,
    UntaggedFile,
)
from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS
from backend.domain.enums import MatchStatus
from backend.domain.system import StorageUnavailableError
from backend.repositories.cache_fill_work import CacheFillWorkRepository

_NOT_LISTED = """NOT EXISTS (SELECT 1 FROM log_name_placeholders p
                             WHERE p.normalized_name = ba.normalized_name)"""

# The (name, normalized title) pairs of the names in a ``names(id)`` CTE.
_PAIRS = """
lines AS (
    SELECT ti.broadcast_artist_id AS name_id, ti.normalized_title, ti.original_title,
           coalesce(sum(sip.play_count), 0)::bigint AS plays
      FROM track_identities ti
      JOIN names n ON n.id = ti.broadcast_artist_id
      LEFT JOIN station_identity_plays sip ON sip.identity_id = ti.id
     WHERE ti.normalized_title <> ''
     GROUP BY ti.id
),
pairs AS (
    SELECT name_id, normalized_title, sum(plays)::bigint AS plays,
           (array_agg(original_title ORDER BY plays DESC, original_title))[1] AS title
      FROM lines
     GROUP BY name_id, normalized_title
)"""

_SEARCHED_PAIRS = f"""
WITH names AS (
    SELECT ba.id FROM broadcast_artists ba
     WHERE ba.match_status = ANY(%(statuses)s) AND {_NOT_LISTED}
),
{_PAIRS},
titles AS (SELECT DISTINCT normalized_title FROM pairs),
held AS (
    SELECT DISTINCT f.normalized_title
      FROM library_files f JOIN titles t ON t.normalized_title = f.normalized_title
     WHERE f.file_status = 'present'
),
acts AS (
    SELECT normalized_title, count(DISTINCT act) AS n
      FROM (
          SELECT f.normalized_title,
                 coalesce('mbid:' || lower(m.mbid),
                          'name:' || coalesce(f.normalized_artist_name, '')) AS act
            FROM library_files f
            JOIN titles t ON t.normalized_title = f.normalized_title
            LEFT JOIN LATERAL unnest(
                CASE WHEN cardinality(f.artist_mbids) > 0 THEN f.artist_mbids
                     WHEN coalesce(f.artist_mbid, '') <> '' THEN ARRAY[f.artist_mbid]
                END) AS m(mbid) ON true
           WHERE f.file_status = 'present'
          UNION ALL
          SELECT ti.normalized_title,
                 CASE WHEN coalesce(a.mbid, '') <> '' AND NOT (a.mbid = ANY(%(special)s))
                      THEN 'mbid:' || lower(a.mbid) ELSE 'artist:' || a.id END
            FROM track_identities ti
            JOIN titles t ON t.normalized_title = ti.normalized_title
            JOIN log_name_links l
              ON l.broadcast_artist_id = ti.broadcast_artist_id AND l.state = 'active'
            JOIN artists a ON a.id = l.artist_id
      ) every_act
     GROUP BY normalized_title
)
SELECT ba.normalized_name, ba.original_name, p.normalized_title, p.title, p.plays,
       h.normalized_title IS NOT NULL AS in_library, coalesce(a.n, 0) >= 3 AS common
  FROM pairs p
  JOIN broadcast_artists ba ON ba.id = p.name_id
  LEFT JOIN held h ON h.normalized_title = p.normalized_title
  LEFT JOIN acts a ON a.normalized_title = p.normalized_title
 ORDER BY p.plays DESC, ba.normalized_name, p.normalized_title
"""

_UNTAGGED_FILES = """
WITH known AS (
    SELECT a.normalized_name AS name FROM artists a
     WHERE coalesce(a.mbid, '') <> '' AND NOT (a.mbid = ANY(%(special)s))
       AND a.normalized_name IS NOT NULL
    UNION
    SELECT n.normalized_name FROM artist_names n JOIN artists a ON a.id = n.artist_id
     WHERE coalesce(a.mbid, '') <> '' AND NOT (a.mbid = ANY(%(special)s))
)
SELECT f.artist_name, f.track_title AS title, count(*) AS files
  FROM library_files f
 WHERE f.file_status = 'present'
   AND coalesce(cardinality(f.artist_mbids), 0) = 0
   AND coalesce(f.artist_mbid, '') = ''
   AND coalesce(f.artist_name, '') <> '' AND coalesce(f.track_title, '') <> ''
   AND NOT EXISTS (SELECT 1 FROM known k WHERE k.name = f.normalized_artist_name)
 GROUP BY f.artist_name, f.track_title
 ORDER BY files DESC, f.artist_name, f.track_title
"""

_MANUAL_TITLES = f"""
WITH manual AS (
    SELECT l.broadcast_artist_id AS id, min(a.mbid) AS mbid
      FROM log_name_links l JOIN artists a ON a.id = l.artist_id
     WHERE l.state = 'active'
     GROUP BY l.broadcast_artist_id
    HAVING count(*) = 1 AND bool_and(l.origin = 'manual')
       AND bool_and(coalesce(a.mbid, '') <> '' AND NOT (a.mbid = ANY(%(special)s)))
),
names AS (
    SELECT ba.id FROM broadcast_artists ba JOIN manual m ON m.id = ba.id
     WHERE ba.match_status <> 'manual_rejected' AND {_NOT_LISTED}
),
{_PAIRS}
SELECT ba.normalized_name, ba.original_name, m.mbid AS artist_mbid, p.title, p.plays
  FROM pairs p
  JOIN broadcast_artists ba ON ba.id = p.name_id
  JOIN manual m ON m.id = p.name_id
 ORDER BY p.plays DESC, ba.normalized_name, p.normalized_title
"""

_CONFIRMATIONS = f"""
SELECT ba.normalized_name, ba.original_name, c.title
  FROM mb_cache_fill_confirmations c
  JOIN broadcast_artists ba ON ba.id = c.broadcast_artist_id
 WHERE ba.match_status <> 'manual_rejected' AND {_NOT_LISTED}
   AND NOT (EXISTS (SELECT 1 FROM log_name_links l
                     WHERE l.broadcast_artist_id = ba.id AND l.state = 'active')
            AND NOT EXISTS (SELECT 1 FROM log_name_links l
                             WHERE l.broadcast_artist_id = ba.id AND l.state = 'active'
                               AND l.origin <> 'manual'))
 ORDER BY c.queued_at, c.id
"""

_CATALOG_MBIDS = """
SELECT DISTINCT mbid FROM artists
 WHERE mbid IS NOT NULL AND mbid <> '' AND NOT (mbid = ANY(%(special)s))
 ORDER BY mbid
"""

# PR 3's work list (S5, R3-b, D15). A recording "performs a work" as the merge list's _LINKED
# reads a ``recording:`` row: its first performance relation naming a work with an id and a
# title. Unlike _LINKED, an expired row is no row here (C42). A release's live ``release:`` row
# carries a recording when one of its tracks, on any medium, is that recording.
_UNLINKED_RECORDINGS = """
WITH unlinked AS (
    SELECT DISTINCT p.recording_mbid
      FROM performances p
      JOIN works w ON w.id = p.work_id
     WHERE coalesce(p.recording_mbid, '') <> ''
       AND (w.origin IS DISTINCT FROM 'musicbrainz' OR w.mbid IS NULL)
       AND NOT EXISTS (
           SELECT 1 FROM mb_cache c
            WHERE c.cache_key = 'recording-work:' || p.recording_mbid AND c.expires_at > now())
       AND NOT EXISTS (
           SELECT 1 FROM mb_cache c
            WHERE c.cache_key = 'recording:' || p.recording_mbid AND c.expires_at > now()
              AND jsonb_path_exists(
                  c.response_data,
                  '$.relations[*] ? (@.type == "performance" && exists(@.work.id)
                                     && exists(@.work.title))'))
)
SELECT DISTINCT u.recording_mbid, f.release_mbid
  FROM unlinked u
  JOIN library_files f ON f.recording_mbid = u.recording_mbid
 WHERE f.file_status = 'present' AND coalesce(f.release_mbid, '') <> ''
   AND NOT EXISTS (
       SELECT 1 FROM mb_cache c
        WHERE c.cache_key = 'release-missing:' || f.release_mbid AND c.expires_at > now())
   AND NOT EXISTS (
       SELECT 1 FROM mb_cache c
        WHERE c.cache_key = 'release:' || f.release_mbid AND c.expires_at > now()
          AND NOT jsonb_path_exists(
              c.response_data,
              '$.media[*].tracks[*].recording.id ? (@ == $rid)',
              jsonb_build_object('rid', u.recording_mbid)))
 ORDER BY f.release_mbid, u.recording_mbid
"""


@translate_lost_connection(StorageUnavailableError)
class PgCacheFillWorkRepository(CacheFillWorkRepository):
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self._conn = conn

    def _rows(self, sql: str, **params: object) -> list[Any]:
        return self._conn.execute(
            sql, {"special": sorted(SPECIAL_PURPOSE_MBIDS), **params}
        ).fetchall()

    def searched_pairs(self, statuses: Collection[MatchStatus]) -> list[NamePair]:
        rows = self._rows(_SEARCHED_PAIRS, statuses=[status.value for status in statuses])
        return [
            NamePair(
                normalized_name=r["normalized_name"],
                original_name=r["original_name"],
                normalized_title=r["normalized_title"],
                title=r["title"],
                plays=int(r["plays"]),
                in_library=bool(r["in_library"]),
                common=bool(r["common"]),
            )
            for r in rows
        ]

    def untagged_files(self) -> list[UntaggedFile]:
        return [
            UntaggedFile(artist_name=r["artist_name"], title=r["title"], files=int(r["files"]))
            for r in self._rows(_UNTAGGED_FILES)
        ]

    def manual_titles(self) -> list[ManualTitle]:
        return [
            ManualTitle(
                normalized_name=r["normalized_name"],
                original_name=r["original_name"],
                artist_mbid=r["artist_mbid"],
                title=r["title"],
                plays=int(r["plays"]),
            )
            for r in self._rows(_MANUAL_TITLES)
        ]

    def confirmations(self) -> list[QueuedPair]:
        return [
            QueuedPair(
                normalized_name=r["normalized_name"],
                original_name=r["original_name"],
                title=r["title"],
            )
            for r in self._rows(_CONFIRMATIONS)
        ]

    def catalog_mbids(self) -> list[str]:
        return [str(r["mbid"]) for r in self._rows(_CATALOG_MBIDS)]

    def unlinked_recordings(self) -> list[UnlinkedRecording]:
        return [
            UnlinkedRecording(
                recording_mbid=str(r["recording_mbid"]), release_mbid=str(r["release_mbid"])
            )
            for r in self._rows(_UNLINKED_RECORDINGS)
        ]
