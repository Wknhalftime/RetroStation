"""E2E integration tests for local-first grouping with real PostgreSQL."""
from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.db.repositories.artists import PgArtistRepository
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.db.repositories.recordings import PgRecordingRepository
from backend.db.repositories.song_masters import PgSongMasterRepository
from backend.db.repositories.works import PgWorkRepository
from backend.domain.enums import SelectionMethod
from backend.domain.models import LibraryFile, SongMaster
from backend.services.grouping_service import assign_work
from backend.services.normalization import normalize_artist, normalize_title
from tests.helpers import assert_grouping_invariants


def _make_file(
    path: str,
    file_hash: str,
    artist: str,
    title: str,
) -> LibraryFile:
    """Build a LibraryFile with normalised fields populated."""
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash=file_hash,
        format="mp3",
        artist_name=artist,
        track_title=title,
        normalized_artist_name=normalize_artist(artist),
        normalized_title=normalize_title(title),
    )


@pytest.mark.integration
class TestGroupingE2E:

    def test_grouping_assigns_work_ids(self, migrated_db: str) -> None:
        """Two similar titles get the same work; a different title gets another."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            artist_repo = PgArtistRepository(conn)
            work_repo = PgWorkRepository(conn)
            file_repo = PgLibraryFileRepository(conn)
            rec_repo = PgRecordingRepository(conn)
            sm_repo = PgSongMasterRepository(conn)

            f1 = _make_file(
                "/music/hey_jude.mp3", "hash1", "Beatles", "Hey Jude",
            )
            f2 = _make_file(
                "/music/hey_jude_remastered.mp3", "hash2",
                "Beatles", "Hey Jude (Remastered)",
            )
            f3 = _make_file(
                "/music/let_it_be.mp3", "hash3", "Beatles", "Let It Be",
            )

            file_repo.upsert(f1)
            file_repo.upsert(f2)
            file_repo.upsert(f3)
            conn.commit()

            # Group each file
            w1 = assign_work(
                f1,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f1.id, w1)
            conn.commit()

            w2 = assign_work(
                f2,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f2.id, w2)
            conn.commit()

            w3 = assign_work(
                f3,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f3.id, w3)
            conn.commit()

            assert w1 is not None
            assert w2 is not None
            assert w3 is not None
            assert w1 == w2, "Same song (remastered) should share a work"
            assert w1 != w3, "Different songs should have different works"

            assert_grouping_invariants(conn)

    def test_hash_shortcut_reuses_work(self, migrated_db: str) -> None:
        """A new file with the same hash inherits the existing work_id."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            artist_repo = PgArtistRepository(conn)
            work_repo = PgWorkRepository(conn)
            file_repo = PgLibraryFileRepository(conn)
            rec_repo = PgRecordingRepository(conn)
            sm_repo = PgSongMasterRepository(conn)

            f1 = _make_file(
                "/music/original.mp3", "same_hash", "Beatles", "Come Together",
            )
            file_repo.upsert(f1)
            conn.commit()

            w1 = assign_work(
                f1,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f1.id, w1)
            conn.commit()

            # Second file with identical hash but different path
            f2 = _make_file(
                "/music/copy.mp3", "same_hash", "Beatles", "Come Together",
            )
            file_repo.upsert(f2)
            conn.commit()

            w2 = assign_work(
                f2,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )

            assert w1 is not None
            assert w2 == w1, "Hash shortcut should reuse the existing work_id"

    def test_rescan_preserves_work_ids(self, migrated_db: str) -> None:
        """Re-upserting the same file (same path, same hash) keeps its work_id."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            artist_repo = PgArtistRepository(conn)
            work_repo = PgWorkRepository(conn)
            file_repo = PgLibraryFileRepository(conn)
            rec_repo = PgRecordingRepository(conn)
            sm_repo = PgSongMasterRepository(conn)

            f1 = _make_file(
                "/music/stable.mp3", "stable_hash", "Beatles", "Yesterday",
            )
            file_repo.upsert(f1)
            conn.commit()

            w1 = assign_work(
                f1,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f1.id, w1)
            conn.commit()

            # Simulate rescan: upsert same path/hash again
            f1_rescan = _make_file(
                "/music/stable.mp3", "stable_hash", "Beatles", "Yesterday",
            )
            f1_rescan.id = uuid4()  # scanner would generate new UUID
            result = file_repo.upsert(f1_rescan)
            conn.commit()

            # ON CONFLICT keeps work_id when hash matches
            assert result.work_id == w1, (
                "Rescan with same hash should preserve work_id"
            )

    def test_merge_works_via_repo(self, migrated_db: str) -> None:
        """Merge two local works: files move, source work deleted, invariants hold."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            artist_repo = PgArtistRepository(conn)
            work_repo = PgWorkRepository(conn)
            file_repo = PgLibraryFileRepository(conn)
            rec_repo = PgRecordingRepository(conn)
            sm_repo = PgSongMasterRepository(conn)

            # Create two separate works via grouping
            f_a = _make_file(
                "/music/song_a.mp3", "hash_a", "Stones", "Paint It Black",
            )
            f_b = _make_file(
                "/music/song_b.mp3", "hash_b", "Stones", "Paint It Blk",
            )

            file_repo.upsert(f_a)
            file_repo.upsert(f_b)
            conn.commit()

            w_a = assign_work(
                f_a,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f_a.id, w_a)
            conn.commit()

            w_b = assign_work(
                f_b,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f_b.id, w_b)
            conn.commit()

            # They may or may not get the same work depending on fuzzy score.
            # If they already matched, create a forced second work for the test.
            if w_a == w_b:
                # Force a distinct second work so we can test merge
                artist_id = artist_repo.upsert_local(
                    "Stones", normalize_artist("Stones"),
                )
                w_b = work_repo.create_local("Paint It Blk", artist_id)
                sm_repo.upsert(SongMaster(
                    id=uuid4(),
                    work_id=w_b,
                    preferred_file_id=f_b.id,
                    selection_method=SelectionMethod.AUTO,
                ))
                file_repo.update_work_id(f_b.id, w_b)
                conn.commit()

            assert w_a is not None
            assert w_b is not None
            assert w_a != w_b

            # Merge w_b -> w_a  (move files, delete source work)
            conn.execute(
                "UPDATE library_files SET work_id = %s WHERE work_id = %s",
                (w_a, w_b),
            )
            conn.execute(
                "DELETE FROM song_masters WHERE work_id = %s", (w_b,),
            )
            conn.execute(
                "DELETE FROM format_overrides WHERE work_id = %s", (w_b,),
            )
            conn.execute(
                "DELETE FROM works WHERE id = %s", (w_b,),
            )
            conn.commit()

            # Verify files moved
            reloaded_b = file_repo.get_by_id(f_b.id)
            assert reloaded_b is not None
            assert reloaded_b.work_id == w_a

            # Source work should be gone
            assert work_repo.get_by_id(w_b) is None

            assert_grouping_invariants(conn)

    def test_split_file_from_work(self, migrated_db: str) -> None:
        """Split one file out of a 2-file work into a new work."""
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            artist_repo = PgArtistRepository(conn)
            work_repo = PgWorkRepository(conn)
            file_repo = PgLibraryFileRepository(conn)
            rec_repo = PgRecordingRepository(conn)
            sm_repo = PgSongMasterRepository(conn)

            # Create two files that group into the same work
            f1 = _make_file(
                "/music/eleanor_rigby.mp3", "hash_er1",
                "Beatles", "Eleanor Rigby",
            )
            f2 = _make_file(
                "/music/eleanor_rigby_live.mp3", "hash_er2",
                "Beatles", "Eleanor Rigby",
            )

            file_repo.upsert(f1)
            file_repo.upsert(f2)
            conn.commit()

            w1 = assign_work(
                f1,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f1.id, w1)
            conn.commit()

            w2 = assign_work(
                f2,
                artist_repo=artist_repo,
                work_repo=work_repo,
                library_file_repo=file_repo,
                recording_repo=rec_repo,
                song_master_repo=sm_repo,
            )
            file_repo.update_work_id(f2.id, w2)
            conn.commit()

            # Both should land in the same work (identical title)
            assert w1 == w2
            original_work_id = w1
            assert original_work_id is not None

            # Split f2 into a new work
            artist_id = artist_repo.upsert_local(
                "Beatles", normalize_artist("Beatles"),
            )
            new_work_id = work_repo.create_local(
                "Eleanor Rigby (Live)", artist_id,
            )
            sm_repo.upsert(SongMaster(
                id=uuid4(),
                work_id=new_work_id,
                preferred_file_id=f2.id,
                selection_method=SelectionMethod.AUTO,
            ))
            file_repo.update_work_id(f2.id, new_work_id)
            conn.commit()

            # Verify split
            reloaded_f1 = file_repo.get_by_id(f1.id)
            reloaded_f2 = file_repo.get_by_id(f2.id)

            assert reloaded_f1 is not None
            assert reloaded_f2 is not None
            assert reloaded_f1.work_id == original_work_id
            assert reloaded_f2.work_id == new_work_id
            assert reloaded_f1.work_id != reloaded_f2.work_id

            assert_grouping_invariants(conn)
