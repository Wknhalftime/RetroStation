"""Integration: identity_resolution_service.recalculate_for_work_sync.

Locks the recalc path's current behaviour (AUD-054) before the
composition-root injection refactor:

- happy path: exactly the given work_id is recalculated and the master row
  is committed and visible on a fresh connection.
- a DB-connection failure is caught and logged as
  ``manual_resolve_recalc_failed_inner``, never raised.
- a non-DB failure (today) is *also* caught and logged the same way — this
  is the behaviour AUD-054's declared change narrows: after psycopg.Error
  narrowing, only the DB-failure test below keeps this expectation.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from structlog.testing import capture_logs

from backend.domain.catalog import Recording
from backend.domain.enums import VersionType
from backend.domain.library import AudioMetadata, LibraryFile
from backend.services import identity_resolution_service, master_selection_service
from backend.services.repository_factory import RepositoryFactory

pytestmark = pytest.mark.integration


def _seed_work_with_file(repos: RepositoryFactory, tmp_path: Path) -> str:
    """Create an artist/work/recording/library_file chain recalc can score."""
    artist_id = repos.artists.upsert_local_artist("Metallica", "metallica")
    work_id = repos.works.create_local("Battery", artist_id)
    recording_id = str(uuid4())
    repos.recordings.upsert(
        Recording(
            id=recording_id,
            title="Battery",
            work_id=work_id,
            version_type=VersionType.ORIGINAL,
            needs_enhancement=False,
        )
    )
    repos.library_files.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=str(tmp_path / "battery.flac"),
            file_hash=None,
            format="flac",
            recording_id=recording_id,
            work_id=work_id,
            audio=AudioMetadata(artist_name="Metallica", track_title="Battery"),
        )
    )
    return work_id


def _bad_db_url(good_db_url: str) -> str:
    """A DSN pointing at a closed local port, for a fast, real connection failure."""
    params = psycopg.conninfo.conninfo_to_dict(good_db_url)
    params["port"] = "1"
    params["connect_timeout"] = "2"
    return psycopg.conninfo.make_conninfo(**params)


class TestRecalculateForWorkSyncHappyPath:
    def test_recalculates_exactly_the_given_work_id_and_commits(
        self, migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            repos = RepositoryFactory(conn)
            target_work_id = _seed_work_with_file(repos, tmp_path)
            # A second work exists so we can prove the recalc call is scoped
            # to exactly [target_work_id], not "all works".
            other_work_id = _seed_work_with_file(repos, tmp_path / "other")
            conn.commit()

        captured_work_ids: list[list[str]] = []
        real_recalculate = master_selection_service.recalculate_song_masters

        def _spy_recalculate(work_ids: list[str], **kwargs: object) -> None:
            captured_work_ids.append(list(work_ids))
            real_recalculate(work_ids, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(master_selection_service, "recalculate_song_masters", _spy_recalculate)
        identity_resolution_service.recalculate_for_work_sync(migrated_db, target_work_id)

        assert captured_work_ids == [[target_work_id]]

        # Commit happened on recalc's own connection — a fresh connection
        # sees the durable master row.
        with psycopg.connect(migrated_db, row_factory=dict_row) as verify_conn:
            row = verify_conn.execute(
                "SELECT work_id FROM song_masters WHERE work_id = %s",
                (target_work_id,),
            ).fetchone()
            assert row is not None

            other_row = verify_conn.execute(
                "SELECT work_id FROM song_masters WHERE work_id = %s",
                (other_work_id,),
            ).fetchone()
            assert other_row is None


class TestRecalculateForWorkSyncFailureSwallowing:
    def test_db_connection_failure_is_logged_inner_and_not_raised(self, migrated_db: str) -> None:
        bad_url = _bad_db_url(migrated_db)

        with capture_logs() as events:
            identity_resolution_service.recalculate_for_work_sync(bad_url, "some-work-id")

        warnings = [e for e in events if e.get("event") == "manual_resolve_recalc_failed_inner"]
        assert len(warnings) == 1
        assert warnings[0]["work_id"] == "some-work-id"
        # exc_info=True so the traceback actually lands in the log record.
        assert warnings[0]["exc_info"] is True

    def test_non_db_failure_is_also_swallowed_today(
        self, migrated_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Today's un-narrowed `except Exception` swallows ANY failure — even
        one that has nothing to do with the database. AUD-054's declared
        behaviour change (narrowing to psycopg.Error) flips this exact
        expectation to "propagates"; see the updated version of this test
        alongside that commit.
        """
        with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
            work_id = _seed_work_with_file(RepositoryFactory(conn), tmp_path)
            conn.commit()

        def _boom(*args: object, **kwargs: object) -> None:
            raise ValueError("not a database problem")

        monkeypatch.setattr(master_selection_service, "recalculate_song_masters", _boom)

        with capture_logs() as events:
            identity_resolution_service.recalculate_for_work_sync(migrated_db, work_id)

        warnings = [e for e in events if e.get("event") == "manual_resolve_recalc_failed_inner"]
        assert len(warnings) == 1
        assert warnings[0]["work_id"] == work_id
        assert warnings[0]["exc_info"] is True
