from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.library_folders import PgLibraryFolderRepository
from backend.domain.models import LibraryFolder


def test_upsert_and_get_by_path(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        folder = LibraryFolder(id=uuid4(), name="jazz", full_path="/music/jazz", folder_hash="abc123")
        repo.upsert(folder)
        conn.commit()
        result = repo.get_by_path("/music/jazz")
        assert result is not None
        assert result.name == "jazz"
        assert result.folder_hash == "abc123"


def test_upsert_updates_existing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        folder = LibraryFolder(id=uuid4(), name="jazz", full_path="/music/jazz2", folder_hash="hash1")
        repo.upsert(folder)
        conn.commit()
        folder.folder_hash = "hash2"
        repo.upsert(folder)
        conn.commit()
        result = repo.get_by_path("/music/jazz2")
        assert result is not None
        assert result.folder_hash == "hash2"


def test_get_children(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        parent = LibraryFolder(id=uuid4(), name="music", full_path="/music")
        child1 = LibraryFolder(id=uuid4(), name="jazz", full_path="/music/jazz", parent_id=parent.id)
        child2 = LibraryFolder(id=uuid4(), name="rock", full_path="/music/rock", parent_id=parent.id)
        repo.upsert(parent)
        repo.upsert(child1)
        repo.upsert(child2)
        conn.commit()
        children = repo.get_children(parent.id)
        assert len(children) == 2
        assert {c.name for c in children} == {"jazz", "rock"}


def test_update_hash(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        folder = LibraryFolder(id=uuid4(), name="test", full_path="/test")
        repo.upsert(folder)
        conn.commit()
        repo.update_hash(folder.id, "newhash")
        conn.commit()
        result = repo.get_by_path("/test")
        assert result is not None
        assert result.folder_hash == "newhash"


def test_stage_and_commit_hashes(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        f1 = LibraryFolder(id=uuid4(), name="a", full_path="/a")
        f2 = LibraryFolder(id=uuid4(), name="b", full_path="/b")
        repo.upsert(f1)
        repo.upsert(f2)
        conn.commit()
        repo.stage_hashes([(f1.id, "hash_a"), (f2.id, "hash_b")], "task-001")
        conn.commit()
        count = repo.commit_staged_hashes("task-001")
        conn.commit()
        assert count == 2
        assert repo.get_by_path("/a").folder_hash == "hash_a"
        assert repo.get_by_path("/b").folder_hash == "hash_b"


def test_staged_hashes_isolated_by_task_id(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        f1 = LibraryFolder(id=uuid4(), name="x", full_path="/x")
        f2 = LibraryFolder(id=uuid4(), name="y", full_path="/y")
        repo.upsert(f1)
        repo.upsert(f2)
        conn.commit()
        repo.stage_hashes([(f1.id, "h1_a")], "task-a")
        repo.stage_hashes([(f2.id, "h2_b")], "task-b")
        conn.commit()
        count = repo.commit_staged_hashes("task-a")
        conn.commit()
        assert count == 1
        assert repo.get_by_path("/x").folder_hash == "h1_a"
        assert repo.get_by_path("/y").folder_hash is None


def test_has_any(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        repo = PgLibraryFolderRepository(conn)
        assert repo.has_any() is False
        folder = LibraryFolder(id=uuid4(), name="z", full_path="/z")
        repo.upsert(folder)
        conn.commit()
        assert repo.has_any() is True
