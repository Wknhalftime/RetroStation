"""Seeds for the cache fill's work links (MusicBrainz cache fill, phase 4.1 PR 3; SPEC S5; R3-b,
R3-e; AUD-R092): works, their performances' recordings, the files that name a release, the
release MusicBrainz answers, and the ``recording-work:`` row the fill writes for a track.

A file is seeded both ways the work list could find it: in its performance (``performance_id``)
and with the performance's recording MBID, so no test pins which of the two the SQL joins on.

MBIDs are made to sort: a release's, a recording's and a work's differ only in their last group,
so releases tie-break by the number they're made from (R3-c).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import DictRow

type Conn = psycopg.Connection[DictRow]

MISSING_SINCE = datetime(2001, 1, 1, 8, tzinfo=UTC)


def release_mbid(n: int) -> str:
    return f"00000001-0000-4000-8000-{n:012x}"


def recording_mbid(n: int) -> str:
    return f"00000002-0000-4000-8000-{n:012x}"


def work_mbid(n: int) -> str:
    return f"00000003-0000-4000-8000-{n:012x}"


# --- what the fill writes and the planner reads --------------------------------------------------


def work_key(recording: str) -> str:
    """The cache key of a recording's fetched work link (R3-e)."""
    return f"recording-work:{recording}"


def linked(work: str, title: str) -> dict[str, Any]:
    """A ``recording-work:`` row naming the work *work* titled *title* (R3-e)."""
    return {"work": {"id": work, "title": title}}


UNLINKED: dict[str, Any] = {"work": None}
"""A ``recording-work:`` row for a track whose recording performs no work MusicBrainz names."""


# --- MusicBrainz's answers -----------------------------------------------------------------------


def performs(work: str, title: str) -> dict[str, Any]:
    """A recording's ``performance`` relation to the work *work*."""
    return {"type": "performance", "work": {"id": work, "title": title}}


def track(recording: str, *relations: dict[str, Any]) -> dict[str, Any]:
    """One track of a release: its recording, with the recording's relations."""
    return {"recording": {"id": recording, "title": "A Song", "relations": list(relations)}}


def release(mbid: str, *media: list[dict[str, Any]]) -> dict[str, Any]:
    """A release lookup's answer (``inc=recordings+...+work-rels``): its media, each a list of
    tracks."""
    return {
        "id": mbid,
        "title": f"Release {mbid[-4:]}",
        "media": [
            {"position": n + 1, "format": "CD", "tracks": tracks} for n, tracks in enumerate(media)
        ],
    }


# --- the library ---------------------------------------------------------------------------------


def _one(row: DictRow | None) -> Any:
    assert row is not None
    return row["id"]


def work(conn: Conn, title: str, *, musicbrainz: str | None = None) -> str:
    """A work titled *title*: the MusicBrainz work *musicbrainz* when given, else a local one."""
    work_id = f"work-{uuid4()}"
    conn.execute(
        "INSERT INTO works (id, title, mbid, origin) VALUES (%s, %s, %s, %s)",
        (work_id, title, musicbrainz, "musicbrainz" if musicbrainz else "local"),
    )
    return work_id


def performance(conn: Conn, work_id: str, recording: str | None) -> UUID:
    """A performance of *work_id*, of the recording *recording* (none when None)."""
    return UUID(
        str(
            _one(
                conn.execute(
                    """INSERT INTO performances (work_id, recording_mbid, version_type)
                       VALUES (%s, %s, 'original') RETURNING id""",
                    (work_id, recording),
                ).fetchone()
            )
        )
    )


def file_on(
    conn: Conn,
    work_id: str,
    performance_id: UUID | None,
    recording: str | None,
    release: str | None,
    *,
    status: str = "present",
) -> UUID:
    """A file of the performance, tagged with *recording* and the release *release*."""
    return UUID(
        str(
            _one(
                conn.execute(
                    """INSERT INTO library_files
                           (file_path, format, file_status, missing_since, work_id,
                            performance_id, recording_mbid, release_mbid)
                       VALUES (%s, 'flac', %s, %s, %s, %s, %s, %s) RETURNING id""",
                    (
                        f"/music/{uuid4().hex}.flac",
                        status,
                        MISSING_SINCE if status == "missing" else None,
                        work_id,
                        performance_id,
                        recording,
                        release,
                    ),
                ).fetchone()
            )
        )
    )


@dataclass(frozen=True)
class Song:
    """A work with one performance of one recording."""

    work_id: str
    performance_id: UUID
    recording: str


def song(
    conn: Conn,
    title: str,
    recording: str,
    *releases: str,
    musicbrainz: str | None = None,
    status: str = "present",
) -> Song:
    """A work titled *title* (the MusicBrainz work *musicbrainz* when given) with a performance
    of *recording*, and one file of it on each of *releases*, in *status*."""
    work_id = work(conn, title, musicbrainz=musicbrainz)
    held = performance(conn, work_id, recording)
    for mbid in releases:
        file_on(conn, work_id, held, recording, mbid, status=status)
    return Song(work_id, held, recording)
