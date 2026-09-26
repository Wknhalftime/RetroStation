"""Integration: PgLibraryFileRepository folds a stale case-duplicate row into its keeper.

Every table with a foreign key to ``library_files`` must be re-pointed
before the stale row is deleted, and a match the keeper already has for
the same broadcast identity must not be doubled (UNIQUE identity/file).
"""
from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.curation import SongMaster
from backend.domain.enums import SelectionMethod
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _file(repos: RepositoryFactory, path: str, work_id: str) -> LibraryFile:
    return repos.library_files.upsert(LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash=str(uuid4()),
        format="flac",
        work_id=work_id,
        audio=AudioMetadata(artist_name="Seal", track_title="Kiss from a Rose"),
    ))


def _identity(repos: RepositoryFactory) -> UUID:
    artist = repos.broadcast_artists.upsert(BroadcastArtist(
        id=uuid4(), original_name="SEAL", normalized_name=str(uuid4()),
    ))
    identity = repos.broadcast_identities.upsert(BroadcastTrackIdentity(
        id=uuid4(), broadcast_artist_id=artist.id,
        original_title="Kiss From A Rose", normalized_title="kiss from a rose",
        normalized_signature=str(uuid4()),
    ))
    return identity.id


def _match(conn: psycopg.Connection[DictRow], identity_id: UUID, file_id: UUID) -> None:
    conn.execute(
        "INSERT INTO matches (identity_id, library_file_id) VALUES (%s, %s)",
        (identity_id, file_id),
    )


def _matched_identities(conn: psycopg.Connection[DictRow], file_id: UUID) -> list[str]:
    rows = conn.execute(
        "SELECT identity_id FROM matches WHERE library_file_id = %s ORDER BY identity_id",
        (file_id,),
    ).fetchall()
    return [str(r["identity_id"]) for r in rows]


def test_merge_into_moves_references_and_deletes_the_stale_row(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Seal", "seal")
        work_id = repos.works.create_local("Kiss from a Rose", artist_id)
        stale = _file(repos, str(tmp_path / "Seal - Kiss from a Rose.flac"), work_id)
        keeper = _file(repos, str(tmp_path / "Seal - Kiss From a Rose.flac"), work_id)

        shared = _identity(repos)  # matched to both rows: the keeper's stays
        moving = _identity(repos)  # matched only to the stale row: moves
        _match(conn, shared, stale.id)
        _match(conn, shared, keeper.id)
        _match(conn, moving, stale.id)
        repos.song_masters.upsert(SongMaster(
            id=uuid4(), work_id=work_id, preferred_file_id=stale.id,
            selection_method=SelectionMethod.AUTO,
        ))
        conn.execute(
            """INSERT INTO format_overrides (work_id, format_name, preferred_file_id)
               VALUES (%s, 'hot_ac', %s)""",
            (work_id, stale.id),
        )

        repos.library_files.merge_into(stale.id, keeper.id)

        assert repos.library_files.get_by_id(stale.id) is None
        assert _matched_identities(conn, keeper.id) == sorted([str(shared), str(moving)])
        master = repos.song_masters.get_by_work(work_id)
        assert master is not None
        assert master.preferred_file_id == keeper.id
        override = conn.execute(
            "SELECT preferred_file_id FROM format_overrides WHERE work_id = %s", (work_id,),
        ).fetchone()
        assert override is not None
        assert override["preferred_file_id"] == keeper.id


def test_case_duplicate_groups_hold_only_paths_equal_ignoring_case(
    migrated_db: str, tmp_path: Path,
) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repos = RepositoryFactory(conn)
        artist_id = repos.artists.upsert_local_artist("Seal", "seal")
        work_id = repos.works.create_local("Kiss from a Rose", artist_id)
        upper = _file(repos, str(tmp_path / "Keep The Faith" / "a.flac"), work_id)
        lower = _file(repos, str(tmp_path / "Keep the Faith" / "a.flac"), work_id)
        _file(repos, str(tmp_path / "Keep the Faith" / "b.flac"), work_id)

        groups = repos.library_files.get_case_duplicate_groups()

        assert [{f.id for f in g} for g in groups] == [{upper.id, lower.id}]
