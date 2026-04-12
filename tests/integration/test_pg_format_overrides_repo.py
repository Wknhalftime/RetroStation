from __future__ import annotations

from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.format_overrides import PgFormatOverrideRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.catalog import Artist, Work
from backend.domain.curation import FormatOverride
from backend.domain.library import LibraryFile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_artist(mbid: str = "artist-fo-001") -> Artist:
    return Artist(id=mbid, name="Test Artist", sort_name="Artist, Test")


def _make_work(mbid: str = "work-fo-001", artist_id: str = "artist-fo-001") -> Work:
    return Work(id=mbid, title="Test Work", artist_id=artist_id)


def _make_file(file_path: str = "/music/track.flac", format: str = "flac") -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=file_path,
        file_hash="abc123",
        format=format,
    )


def _seed_chain(
    conn: psycopg.Connection[dict],
    artist_mbid: str = "artist-fo-001",
    work_mbid: str = "work-fo-001",
    file_path: str = "/music/track.flac",
) -> tuple[str, str, LibraryFile]:
    """Insert artist → work → library_file chain; return (artist_id, work_id, file)."""
    PgArtistRepository(conn).upsert(_make_artist(artist_mbid))
    PgWorkRepository(conn).upsert(_make_work(work_mbid, artist_id=artist_mbid))
    lf = PgLibraryFileRepository(conn).upsert(_make_file(file_path))
    conn.commit()
    return artist_mbid, work_mbid, lf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCreateAndList:
    def test_create_returns_persisted_model(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, lf = _seed_chain(conn)

            repo = PgFormatOverrideRepository(conn)
            override = FormatOverride(
                id=uuid4(),
                work_id=work_id,
                format_name="flac",
                preferred_file_id=lf.id,
                notes="prefer lossless",
            )
            created = repo.create(override)

            assert created.id == override.id
            assert created.work_id == work_id
            assert created.format_name == "flac"
            assert created.preferred_file_id == lf.id
            assert created.notes == "prefer lossless"
            assert created.created_at is not None
            conn.commit()

    def test_list_by_work_returns_all_overrides(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, lf = _seed_chain(conn)

            # Need a second file for the second override
            lf2 = PgLibraryFileRepository(conn).upsert(
                _make_file("/music/track.mp3", format="mp3")
            )
            conn.commit()

            repo = PgFormatOverrideRepository(conn)
            repo.create(
                FormatOverride(
                    id=uuid4(),
                    work_id=work_id,
                    format_name="flac",
                    preferred_file_id=lf.id,
                )
            )
            repo.create(
                FormatOverride(
                    id=uuid4(),
                    work_id=work_id,
                    format_name="mp3",
                    preferred_file_id=lf2.id,
                )
            )
            conn.commit()

            results = repo.list_by_work(work_id)
            assert len(results) == 2
            # Ordered by format_name
            assert results[0].format_name == "flac"
            assert results[1].format_name == "mp3"

    def test_list_by_work_empty(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, _ = _seed_chain(conn)

            repo = PgFormatOverrideRepository(conn)
            results = repo.list_by_work(work_id)
            assert results == []


class TestGet:
    def test_get_existing(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, lf = _seed_chain(conn)

            repo = PgFormatOverrideRepository(conn)
            override = FormatOverride(
                id=uuid4(),
                work_id=work_id,
                format_name="flac",
                preferred_file_id=lf.id,
            )
            repo.create(override)
            conn.commit()

            fetched = repo.get(work_id, "flac")
            assert fetched is not None
            assert fetched.id == override.id
            assert fetched.preferred_file_id == lf.id

    def test_get_missing_returns_none(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, _ = _seed_chain(conn)

            repo = PgFormatOverrideRepository(conn)
            result = repo.get(work_id, "nonexistent-format")
            assert result is None

    def test_get_wrong_work_returns_none(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, lf = _seed_chain(conn)

            repo = PgFormatOverrideRepository(conn)
            repo.create(
                FormatOverride(
                    id=uuid4(),
                    work_id=work_id,
                    format_name="flac",
                    preferred_file_id=lf.id,
                )
            )
            conn.commit()

            result = repo.get("nonexistent-work-id", "flac")
            assert result is None


class TestDelete:
    def test_delete_removes_row(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, lf = _seed_chain(conn)

            repo = PgFormatOverrideRepository(conn)
            override_id = uuid4()
            repo.create(
                FormatOverride(
                    id=override_id,
                    work_id=work_id,
                    format_name="flac",
                    preferred_file_id=lf.id,
                )
            )
            conn.commit()

            assert repo.get(work_id, "flac") is not None
            repo.delete(override_id)
            conn.commit()

            assert repo.get(work_id, "flac") is None

    def test_delete_nonexistent_is_noop(self, migrated_db: str) -> None:
        """Deleting a non-existent id should not raise."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repo = PgFormatOverrideRepository(conn)
            # Should not raise
            repo.delete(uuid4())
            conn.commit()

    def test_delete_only_removes_target(self, migrated_db: str) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            _, work_id, lf = _seed_chain(conn)
            lf2 = PgLibraryFileRepository(conn).upsert(
                _make_file("/music/track.mp3", format="mp3")
            )
            conn.commit()

            repo = PgFormatOverrideRepository(conn)
            id1 = uuid4()
            id2 = uuid4()
            repo.create(
                FormatOverride(
                    id=id1,
                    work_id=work_id,
                    format_name="flac",
                    preferred_file_id=lf.id,
                )
            )
            repo.create(
                FormatOverride(
                    id=id2,
                    work_id=work_id,
                    format_name="mp3",
                    preferred_file_id=lf2.id,
                )
            )
            conn.commit()

            repo.delete(id1)
            conn.commit()

            remaining = repo.list_by_work(work_id)
            assert len(remaining) == 1
            assert remaining[0].format_name == "mp3"
