"""Syrupy characterisation snapshots for ``get_work_detail`` (AUD-004 gate 1).

Locks the full ``WorkDetail`` JSON body for one rich real work (two
recordings, one of them with no files; an orphan pseudo-recording; a manual
song master; two ordered format overrides) and for one synthetic work id
(``syn_...``, decoded from library_files directly, no work/recording rows).

All ids are fixed so the snapshot never perturbs itself. ``song_masters`` and
``format_overrides`` timestamps come from the database's ``now()``/
``created_at`` defaults, which are not reproducible across runs; they are
scrubbed to a constant placeholder before the JSON is compared.

Must stay byte-identical through the AUD-004 refactor (``get_work_detail``
splitting into ``_real_work_detail`` / ``_synthetic_work_detail``, and the
three ``FileInfo(...)`` row mappings merging into one helper).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from syrupy.assertion import SnapshotAssertion

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.catalog import Artist, Recording, Work
from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import EnrichmentStatus, SelectionMethod, VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.synthetic_work_id import encode as encode_synthetic_work_id

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from psycopg import Connection

_TIMESTAMP_KEYS = frozenset({"updated_at", "created_at"})
_TIMESTAMP_PLACEHOLDER = "<TIMESTAMP>"

_ARTIST_ID = "a-snapshot-rich"
_WORK_ID = "w-snapshot-rich"
_REC_WITH_FILES = "r-snapshot-with-files"
_REC_NO_FILES = "r-snapshot-no-files"
_FILE_A = UUID("00000000-0000-4000-8000-0000000000a1")
_FILE_B = UUID("00000000-0000-4000-8000-0000000000a2")
_ORPHAN_FILE = UUID("00000000-0000-4000-8000-0000000000a3")
_SONG_MASTER_ID = UUID("00000000-0000-4000-8000-0000000000b1")
_OVERRIDE_FLAC_ID = UUID("00000000-0000-4000-8000-0000000000c1")
_OVERRIDE_MP3_ID = UUID("00000000-0000-4000-8000-0000000000c2")

_SYNTHETIC_ARTIST_ID = "a-snapshot-synthetic"
_SYNTHETIC_TRACK_TITLE = "Snapshot Synthetic Track"
_SYNTHETIC_FILE = UUID("00000000-0000-4000-8000-0000000000d1")


def _scrub_timestamps(value: Any) -> Any:
    """Replace every ``updated_at``/``created_at`` value with a fixed placeholder.

    Those columns are populated from the database's ``now()``, so they differ
    on every test run; the rest of the ``WorkDetail`` body is fully
    deterministic and must stay byte-identical.
    """
    if isinstance(value, dict):
        return {
            key: (_TIMESTAMP_PLACEHOLDER if key in _TIMESTAMP_KEYS else _scrub_timestamps(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [_scrub_timestamps(item) for item in value]
    return value


def _make_file(
    file_id: UUID,
    file_path: str,
    *,
    recording_id: str | None,
    work_id: str | None,
    duration_ms: int,
    album_artist_mbid: str | None = None,
    track_title: str = "Track Title",
) -> LibraryFile:
    return LibraryFile(
        id=file_id,
        file_path=file_path,
        format="flac",
        enrichment_status=EnrichmentStatus.PENDING,
        recording_id=recording_id,
        work_id=work_id,
        audio=AudioMetadata(
            track_title=track_title,
            album_artist_mbid=album_artist_mbid,
            release_title="Snapshot Album",
            bitrate=320,
            duration_ms=duration_ms,
        ),
    )


def test_rich_real_work_detail_snapshot(
    client: TestClient, db_conn: Connection, snapshot: SnapshotAssertion
) -> None:
    """Two recordings (one with no files), an orphan bucket, a master, overrides."""
    PgArtistRepository(db_conn).upsert(
        Artist(id=_ARTIST_ID, name="Snapshot Artist", sort_name="Snapshot Artist")
    )
    PgWorkRepository(db_conn).upsert(Work(id=_WORK_ID, title="Snapshot Work", artist_id=_ARTIST_ID))
    PgRecordingRepository(db_conn).upsert(
        Recording(id=_REC_WITH_FILES, title="Recording With Files", work_id=_WORK_ID)
    )
    PgRecordingRepository(db_conn).upsert(
        Recording(
            id=_REC_NO_FILES,
            title="Recording With No Files",
            work_id=_WORK_ID,
            version_type=VersionType.LIVE,
        )
    )
    PgLibraryFileRepository(db_conn).upsert(
        _make_file(
            _FILE_B,
            "/music/snapshot/b.flac",
            recording_id=_REC_WITH_FILES,
            work_id=_WORK_ID,
            duration_ms=222_000,
        )
    )
    PgLibraryFileRepository(db_conn).upsert(
        _make_file(
            _FILE_A,
            "/music/snapshot/a.flac",
            recording_id=_REC_WITH_FILES,
            work_id=_WORK_ID,
            duration_ms=111_000,
        )
    )
    PgLibraryFileRepository(db_conn).upsert(
        _make_file(
            _ORPHAN_FILE,
            "/music/snapshot/orphan.flac",
            recording_id=None,
            work_id=_WORK_ID,
            duration_ms=333_000,
        )
    )
    PgSongMasterRepository(db_conn).upsert(
        SongMaster(
            id=_SONG_MASTER_ID,
            work_id=_WORK_ID,
            preferred_file_id=_FILE_A,
            selection_method=SelectionMethod.MANUAL,
        )
    )
    PgFormatOverrideRepository(db_conn).create(
        FormatOverride(
            id=_OVERRIDE_MP3_ID,
            work_id=_WORK_ID,
            format_name="mp3",
            preferred_file_id=_FILE_A,
            notes="mp3 override",
        )
    )
    PgFormatOverrideRepository(db_conn).create(
        FormatOverride(
            id=_OVERRIDE_FLAC_ID,
            work_id=_WORK_ID,
            format_name="flac",
            preferred_file_id=_FILE_B,
            notes=None,
        )
    )
    db_conn.commit()

    resp = client.get(f"/api/v1/library/works/{_WORK_ID}")
    assert resp.status_code == 200
    assert _scrub_timestamps(resp.json()) == snapshot


def test_synthetic_work_detail_snapshot(
    client: TestClient, db_conn: Connection, snapshot: SnapshotAssertion
) -> None:
    """A synthetic id decoded straight from library_files, no work/recording rows."""
    PgArtistRepository(db_conn).upsert(
        Artist(
            id=_SYNTHETIC_ARTIST_ID,
            name="Snapshot Synthetic Artist",
            sort_name="Snapshot Synthetic Artist",
        )
    )
    PgLibraryFileRepository(db_conn).upsert(
        _make_file(
            _SYNTHETIC_FILE,
            "/music/snapshot/synthetic.flac",
            recording_id=None,
            work_id=None,
            duration_ms=456_000,
            album_artist_mbid=_SYNTHETIC_ARTIST_ID,
            track_title=_SYNTHETIC_TRACK_TITLE,
        )
    )
    db_conn.commit()

    synthetic_id = encode_synthetic_work_id(_SYNTHETIC_ARTIST_ID, _SYNTHETIC_TRACK_TITLE)
    resp = client.get(f"/api/v1/library/works/{synthetic_id}")
    assert resp.status_code == 200
    assert _scrub_timestamps(resp.json()) == snapshot
