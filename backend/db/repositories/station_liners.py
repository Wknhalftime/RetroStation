from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import psycopg
from psycopg.rows import DictRow

from backend.db.repositories._pg_utils import translate_lost_connection
from backend.db.repositories.stream_cues import row_analysis
from backend.domain.broadcast import BroadcastStorageError
from backend.domain.library import AudioHash
from backend.domain.liners import (
    DuplicateLinerError,
    LinerEntry,
    PlayableLiner,
    StationLiner,
    liner_cue_state,
    liner_markers,
    liner_points,
)
from backend.domain.streaming import ClipFormat, CueAnalysis, CueCandidate, CueMarkers
from backend.repositories.station_liners import (
    LinerCueWorkRepository,
    PlayableLinerRepository,
    StationLinerRepository,
)

#: The unique key a second copy of the same audio in one station violates (LR3); keyed by
#: name, so an unrelated unique violation is never mistaken for a duplicate.
_DUPLICATE_CONSTRAINT = "station_liners_station_id_audio_hash_key"

_INSERT_SQL = """
INSERT INTO station_liners (id, station_id, name, file_name, format, audio_hash, duration_ms,
                            cue_start_ms, cue_eom_ms, cue_stop_ms, uploaded_at)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_GET_SQL = "SELECT * FROM station_liners WHERE id = %s"

_FIND_SQL = "SELECT * FROM station_liners WHERE station_id = %s AND audio_hash = %s"

_LIST_SQL = """
SELECT * FROM station_liners WHERE station_id = %s ORDER BY lower(name), uploaded_at
"""

_CUES_SQL = "SELECT * FROM stream_cues WHERE audio_hash = ANY(%s)"

_RENAME_SQL = "UPDATE station_liners SET name = %s WHERE id = %s"

_MARKERS_SQL = """
UPDATE station_liners SET cue_start_ms = %s, cue_eom_ms = %s, cue_stop_ms = %s WHERE id = %s
"""

_DELETE_SQL = "DELETE FROM station_liners WHERE id = %s"

# Needs analysis (D20, LR4): a liner whose audio has no cue row.
_NEEDING_ANALYSIS_SQL = """
SELECT l.id, l.station_id, l.file_name, l.audio_hash, l.duration_ms
FROM station_liners l
WHERE NOT EXISTS (SELECT 1 FROM stream_cues c WHERE c.audio_hash = l.audio_hash)
ORDER BY l.station_id, l.file_name
"""

# The stream's read (LR4): each liner with its audio's cue row, if any. ``c.*`` supplies the
# row ``row_analysis`` reads (its ``audio_hash`` is NULL when there is none), so the liner's
# own columns are listed by name and never shadow it. Upload order, then id.
_PLAYABLE_SQL = """
SELECT l.id AS liner_id, l.file_name, l.duration_ms, l.cue_start_ms, l.cue_eom_ms,
       l.cue_stop_ms, c.*
FROM station_liners l
LEFT JOIN stream_cues c ON c.audio_hash = l.audio_hash
WHERE l.station_id = %s
ORDER BY l.uploaded_at, l.id
"""


def _markers(row: DictRow) -> CueMarkers | None:
    """The liner's edit; the table's CHECK keeps the three columns all null or all set."""
    if row["cue_start_ms"] is None:
        return None
    return liner_markers(row["cue_start_ms"], row["cue_eom_ms"], row["cue_stop_ms"])


def _to_liner(row: DictRow) -> StationLiner:
    return StationLiner(
        id=row["id"],
        station_id=row["station_id"],
        name=row["name"],
        file_name=row["file_name"],
        format=ClipFormat(row["format"]),
        audio_hash=AudioHash.parse(row["audio_hash"]),
        duration_ms=row["duration_ms"],
        markers=_markers(row),
        uploaded_at=row["uploaded_at"],
    )


@translate_lost_connection(BroadcastStorageError)
class PgStationLinerRepository(StationLinerRepository, LinerCueWorkRepository):
    """PostgreSQL implementation of the liner ports (table ``station_liners``).

    A liner's cue state reads its audio's ``stream_cues`` row; its edit is its own three
    columns, never ``stream_cue_edits`` (edits are per liner, not per audio).
    """

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def add(self, liner: StationLiner) -> None:
        markers = liner.markers
        params = (
            liner.id,
            liner.station_id,
            liner.name,
            liner.file_name,
            liner.format.value,
            str(liner.audio_hash),
            liner.duration_ms,
            None if markers is None else markers.start_ms,
            None if markers is None else markers.eom_ms,
            None if markers is None else markers.stop_ms,
            liner.uploaded_at,
        )
        try:
            self._conn.execute(_INSERT_SQL, params)
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name != _DUPLICATE_CONSTRAINT:
                raise
            raise DuplicateLinerError(
                f"station {liner.station_id} already has a liner with {liner.audio_hash}"
            ) from exc

    def get(self, liner_id: UUID) -> StationLiner | None:
        row = self._conn.execute(_GET_SQL, (liner_id,)).fetchone()
        return None if row is None else _to_liner(row)

    def find_by_hash(self, station_id: UUID, audio_hash: AudioHash) -> StationLiner | None:
        row = self._conn.execute(_FIND_SQL, (station_id, str(audio_hash))).fetchone()
        return None if row is None else _to_liner(row)

    def list_for_station(self, station_id: UUID) -> list[LinerEntry]:
        liners = [_to_liner(r) for r in self._conn.execute(_LIST_SQL, (station_id,))]
        stored = self._analyses([str(liner.audio_hash) for liner in liners])
        return [
            LinerEntry(liner, liner_cue_state(liner.markers, stored.get(liner.audio_hash)))
            for liner in liners
        ]

    def _analyses(self, audio_hashes: list[str]) -> dict[AudioHash, CueAnalysis]:
        """The valid ``stream_cues`` rows of ``audio_hashes``, by audio."""
        if not audio_hashes:
            return {}
        rows = self._conn.execute(_CUES_SQL, (audio_hashes,)).fetchall()
        found = (row_analysis(row) for row in rows)
        return {a.audio_hash: a for a in found if a is not None}

    def rename(self, liner_id: UUID, name: str) -> None:
        liner = self.get(liner_id)
        if liner is None:
            return
        # replace() re-runs StationLiner's rules: the name is stripped, or refused.
        renamed = replace(liner, name=name)
        self._conn.execute(_RENAME_SQL, (renamed.name, liner_id))

    def save_markers(self, liner_id: UUID, markers: CueMarkers | None) -> None:
        liner = self.get(liner_id)
        if liner is None:
            return
        # replace() refuses a fade not at Stop and a Stop past the duration, so the
        # table's CHECK is never what refuses an edit.
        edit = replace(liner, markers=markers).markers
        if edit is None:
            params: tuple[object, ...] = (None, None, None, liner_id)
        else:
            params = (edit.start_ms, edit.eom_ms, edit.stop_ms, liner_id)
        self._conn.execute(_MARKERS_SQL, params)

    def delete(self, liner_id: UUID) -> None:
        self._conn.execute(_DELETE_SQL, (liner_id,))

    def needing_analysis(self) -> list[CueCandidate]:
        return [
            CueCandidate(
                file_id=row["id"],
                path=f"{row['station_id']}/{row['file_name']}",
                audio_hash=AudioHash.parse(row["audio_hash"]),
                duration_ms=row["duration_ms"],
                file_size=None,
                file_mtime_ns=None,
            )
            for row in self._conn.execute(_NEEDING_ANALYSIS_SQL)
        ]


def _to_playable(row: DictRow) -> PlayableLiner | None:
    """The liner the joined row describes, or None when its audio is silent. An invalid cue
    row (logged by ``row_analysis``) counts as none, as the liners tab reads it."""
    stored = None if row["audio_hash"] is None else row_analysis(row)
    points = liner_points(row["duration_ms"], _markers(row), stored)
    return None if points is None else PlayableLiner(row["liner_id"], row["file_name"], points)


class PgPlayableLinerRepository(PlayableLinerRepository):
    """PostgreSQL implementation of :class:`PlayableLinerRepository`: one statement, the
    station's liners LEFT JOIN ``stream_cues`` by audio hash."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self._conn = conn

    def playable(self, station_id: UUID) -> list[PlayableLiner]:
        rows = self._conn.execute(_PLAYABLE_SQL, (station_id,))
        found = (_to_playable(row) for row in rows)
        return [liner for liner in found if liner is not None]
