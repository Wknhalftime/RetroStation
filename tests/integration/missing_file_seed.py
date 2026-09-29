"""Pg seed helpers for PR C's Missing Files tests, frozen with them.

Imports only code that exists on master before PR C, so any task's tests may use it.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg

from backend.db.repositories.task_progress import PgTaskProgressRepository
from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.enums import MatchStatus
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.library_scan_tasks import _run_scan

Conn = psycopg.Connection[dict[str, Any]]
AUDIO = Path(__file__).parent.parent / "fixtures" / "audio"


def put(fixture: str, dest: Path) -> Path:
    """Copy a fixture audio file to *dest*, creating its folder."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(AUDIO / fixture, dest)
    return dest


def retag(path: Path) -> None:
    """What a tagger does to an MP3: new tag content, so a new size and mtime."""
    from mutagen.id3 import COMM, ID3

    tags = ID3(str(path))
    tags.add(COMM(encoding=3, lang="eng", desc="", text="retagged by Picard"))
    tags.save()


def full_scan(conn: Conn, root: Path) -> None:
    """The full "Scan Library" scan of *root*, committed."""
    _run_scan(
        root_path=str(root),
        library_conn=conn,
        repos=RepositoryFactory(conn),
        progress_repo=PgTaskProgressRepository(conn),
        task_id=uuid4().hex,
    )
    conn.commit()


def work(repos: RepositoryFactory, title: str = "Kiss") -> str:
    """A local work by Prince."""
    return repos.works.create_local(title, repos.artists.upsert_local_artist("Prince", "prince"))


def library_file(
    repos: RepositoryFactory,
    path: str,
    work_id: str | None,
    *,
    missing: bool,
    audio: AudioMetadata | None = None,
) -> LibraryFile:
    """A FLAC row at *path*, then marked missing when asked. Returns the row as upserted."""
    row = repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            format="flac",
            work_id=work_id,
            audio=audio or AudioMetadata(),
        )
    )
    if missing:
        repos.library_files.mark_missing(path)
    return row


def identity(repos: RepositoryFactory, status: MatchStatus = MatchStatus.AUTO_MATCHED) -> UUID:
    """A broadcast track identity, with an artist of its own, in *status*."""
    artist = repos.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="PRINCE", normalized_name=str(uuid4()))
    )
    return repos.broadcast_identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="Kiss",
            normalized_title="kiss",
            normalized_signature=str(uuid4()),
            match_status=status,
        )
    ).id


def match(conn: Conn, identity_id: UUID, file_id: UUID, work_id: str | None = None) -> None:
    """An identity match naming *file_id*."""
    conn.execute(
        "INSERT INTO matches (identity_id, library_file_id, work_id) VALUES (%s, %s, %s)",
        (identity_id, file_id, work_id),
    )
