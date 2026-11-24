"""PostgreSQL adapter for the merge list's reads (work merges phase 3b PR 4; AUD-R080)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.db.repositories.artist_merges import MERGE_ROOTS_CTE
from backend.domain.catalog import CatalogStorageError
from backend.domain.merge_list import LoggedFailure, MasterView, RejectingLine, WorkView
from backend.repositories.merge_list import MergeListRepository, PlanCredit, PlanWork

# The work a cached recording lookup says it performs: the first "performance" relation naming a
# work with an id and a title (``library_enrichment_service.performed_work_of``). And the work a
# ``recording-work:`` row names: the cache fill's link from a release lookup (phase 4.1 PR 3, S5,
# R3-g; AUD-R092), ``{"work": null}`` when the track performs none. Neither arm reads a
# ``release:`` row (R1), and both read a row whatever its ``expires_at``.
_LINKED = """
SELECT DISTINCT p.work_id,
       jsonb_path_query_first(
           c.response_data,
           '$.relations[*] ? (@.type == "performance" && exists(@.work.id)
                              && exists(@.work.title)).work.id'
       ) #>> '{}' AS work_mbid
  FROM performances p
  JOIN mb_cache c ON c.cache_key = 'recording:' || p.recording_mbid
 WHERE p.recording_mbid IS NOT NULL
UNION
SELECT p.work_id, c.response_data #>> '{work,id}' AS work_mbid
  FROM performances p
  JOIN mb_cache c ON c.cache_key = 'recording-work:' || p.recording_mbid
 WHERE p.recording_mbid IS NOT NULL
"""

# A credit by MBID alone finds its artist through the MBID (readers join artists on it, 3a D1).
_CREDITS = """
SELECT p.work_id, COALESCE(pc.artist_id, a.id) AS artist_id, COALESCE(pc.mbid, a.mbid) AS mbid,
       pc.credited_name, a.name AS artist_name
  FROM performance_credits pc
  JOIN performances p ON p.id = pc.performance_id
  LEFT JOIN artists a
    ON a.id = pc.artist_id OR (pc.artist_id IS NULL AND a.mbid = pc.mbid)
"""

_PLAYS = """
WITH touched AS (
    SELECT DISTINCT m.identity_id
      FROM matches m
      JOIN library_files f ON f.id = m.library_file_id
      LEFT JOIN recordings r ON r.id = f.recording_id
     WHERE f.work_id = ANY(%(works)s) OR (f.work_id IS NULL AND r.work_id = ANY(%(works)s))
),
lines AS (
    SELECT t.identity_id, COALESCE(f.work_id, r.work_id) AS work_id
      FROM touched t
      JOIN track_identities ti
        ON ti.id = t.identity_id AND ti.match_status IN ('auto_matched', 'manual_matched')
     CROSS JOIN LATERAL (
         SELECT m.library_file_id FROM matches m
          WHERE m.identity_id = t.identity_id AND m.library_file_id IS NOT NULL
          ORDER BY m.confidence_score DESC, m.created_at, m.id
          LIMIT 1
     ) best
      JOIN library_files f ON f.id = best.library_file_id
      LEFT JOIN recordings r ON r.id = f.recording_id
)
SELECT l.work_id, sum(sip.play_count) AS plays
  FROM lines l
  JOIN station_identity_plays sip ON sip.identity_id = l.identity_id
 WHERE l.work_id = ANY(%(works)s)
 GROUP BY l.work_id
"""

_SHOWN = """
SELECT w.id, w.title, w.origin = 'musicbrainz' AS musicbrainz,
       sm.preferred_file_id, sm.selection_method, f.file_path,
       EXISTS (SELECT 1 FROM stream_cue_edits e WHERE e.audio_hash = f.audio_hash) AS cue_edited
  FROM works w
  LEFT JOIN song_masters sm ON sm.work_id = w.id
  LEFT JOIN library_files f ON f.id = sm.preferred_file_id
 WHERE w.id = ANY(%s)
"""

_SHOWN_ARTISTS = """
SELECT p.work_id, COALESCE(pc.credited_name, a.name, pc.mbid) AS name
  FROM performance_credits pc
  JOIN performances p ON p.id = pc.performance_id
  LEFT JOIN artists a ON a.id = pc.artist_id
 WHERE p.work_id = ANY(%s)
 ORDER BY p.work_id, pc.position, p.id
"""

_REJECTIONS = """
SELECT DISTINCT ti.id, ti.original_title AS title, ba.original_name AS artist, f.work_id
  FROM track_identities ti
  JOIN broadcast_artists ba ON ba.id = ti.broadcast_artist_id
  JOIN library_files f ON f.id = ANY(ti.rejected_file_ids)
 WHERE ti.rejected_file_ids <> '{}' AND f.work_id = ANY(%s)
 ORDER BY ti.id, f.work_id
"""


@translate_lost_connection(CatalogStorageError)
class PgMergeListRepository(MergeListRepository):
    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def plan_works(self) -> list[PlanWork]:
        linked: dict[str, set[str]] = defaultdict(set)
        for row in self._conn.execute(_LINKED).fetchall():
            if row["work_mbid"]:
                linked[str(row["work_id"])].add(str(row["work_mbid"]))
        credits: dict[str, list[PlanCredit]] = defaultdict(list)
        for row in self._conn.execute(_CREDITS).fetchall():
            names = tuple(n for n in (row["credited_name"], row["artist_name"]) if n)
            credits[str(row["work_id"])].append(
                PlanCredit(artist_id=row["artist_id"], mbid=row["mbid"], names=names)
            )
        rows = self._conn.execute(
            """SELECT id, title, CASE WHEN origin = 'musicbrainz' THEN mbid END AS mbid
               FROM works"""
        ).fetchall()
        return [
            PlanWork(
                work_id=str(r["id"]),
                title=str(r["title"] or ""),
                work_mbid=r["mbid"],
                linked_mbids=frozenset(linked.get(str(r["id"]), ())),
                credits=tuple(credits.get(str(r["id"]), ())),
            )
            for r in rows
        ]

    def primaries(self, artist_ids: Iterable[str]) -> dict[str, str]:
        rows = self._conn.execute(
            f"""WITH RECURSIVE {MERGE_ROOTS_CTE}
                SELECT artist_id, primary_id FROM merge_roots WHERE artist_id = ANY(%s)""",
            (sorted(set(artist_ids)),),
        ).fetchall()
        return {str(r["artist_id"]): str(r["primary_id"]) for r in rows}

    def matched_plays(self, work_ids: Sequence[str]) -> dict[str, int]:
        rows = self._conn.execute(_PLAYS, {"works": list(work_ids)}).fetchall()
        return {str(r["work_id"]): int(r["plays"]) for r in rows}

    def works_shown(self, work_ids: Sequence[str]) -> dict[str, WorkView]:
        artists: dict[str, list[str]] = defaultdict(list)
        for row in self._conn.execute(_SHOWN_ARTISTS, (list(work_ids),)).fetchall():
            named = artists[str(row["work_id"])]
            if row["name"] and row["name"] not in named:
                named.append(str(row["name"]))
        shown: dict[str, WorkView] = {}
        for r in self._conn.execute(_SHOWN, (list(work_ids),)).fetchall():
            master = (
                None
                if r["preferred_file_id"] is None
                else MasterView(
                    file_id=UUID(str(r["preferred_file_id"])),
                    file_path=r["file_path"],
                    method=str(r["selection_method"]),
                )
            )
            shown[str(r["id"])] = WorkView(
                work_id=str(r["id"]),
                title=str(r["title"] or ""),
                artists=tuple(artists.get(str(r["id"]), ())),
                plays=0,
                musicbrainz=bool(r["musicbrainz"]),
                master=master,
                cue_edited=bool(r["cue_edited"]),
            )
        return shown

    def rejections(self, work_ids: Sequence[str]) -> list[RejectingLine]:
        rows = self._conn.execute(_REJECTIONS, (list(work_ids),)).fetchall()
        return [
            RejectingLine(
                identity_id=UUID(str(r["id"])),
                title=str(r["title"]),
                artist=str(r["artist"]),
                work_id=str(r["work_id"]),
            )
            for r in rows
        ]

    def failures(self, target_ids: Sequence[str]) -> list[LoggedFailure]:
        rows = self._conn.execute(
            """SELECT target_id, source_ids, error, logged_at FROM work_merges
               WHERE status = 'failed' AND target_id = ANY(%s)
               ORDER BY logged_at DESC, id""",
            (list(target_ids),),
        ).fetchall()
        return [
            LoggedFailure(
                target_id=str(r["target_id"]),
                source_ids=frozenset(str(s) for s in r["source_ids"]),
                error=str(r["error"] or ""),
                logged_at=r["logged_at"],
            )
            for r in rows
        ]

    def live_ids(self, work_ids: Sequence[str]) -> dict[str, str]:
        rows = self._conn.execute(
            """SELECT x.id, COALESCE(w.id, r.new_id) AS live
               FROM unnest(%s::text[]) AS x(id)
               LEFT JOIN works w ON w.id = x.id
               LEFT JOIN work_redirects r ON r.old_id = x.id""",
            (list(work_ids),),
        ).fetchall()
        return {str(r["id"]): str(r["live"]) for r in rows if r["live"] is not None}
