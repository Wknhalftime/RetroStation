"""FakeWorkRepository and FakeRecordingRepository must mirror the
enhancement-error filter applied to PgArtistRepository.list_unenhanced so
a row flagged with `enhancement_error` stays out of the retry queue.

Currently no production code path writes enhancement_error for works or
recordings; the filter is defensive/forward-safe. These tests pin the
fake behaviour so a future feature that adds quarantine writes doesn't
silently re-queue failed rows.
"""
from __future__ import annotations

from backend.domain.catalog import CatalogSource, Recording, VersionType, Work
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.works import FakeWorkRepository


def _work(
    work_id: str,
    *,
    needs_enhancement: bool = True,
    enhancement_error: str | None = None,
) -> Work:
    return Work(
        id=work_id,
        title=f"Work {work_id}",
        artist_id="artist-uuid",
        needs_enhancement=needs_enhancement,
        enhancement_error=enhancement_error,
        mbid=None,
        origin=CatalogSource.LOCAL,
    )


def _recording(
    rec_id: str,
    *,
    needs_enhancement: bool = True,
    enhancement_error: str | None = None,
) -> Recording:
    return Recording(
        id=rec_id,
        title=f"Recording {rec_id}",
        work_id=None,
        version_type=VersionType.ORIGINAL,
        needs_enhancement=needs_enhancement,
        enhancement_error=enhancement_error,
    )


def test_work_with_enhancement_error_is_excluded_from_queue() -> None:
    repo = FakeWorkRepository()
    repo._data["ok"] = _work("ok")
    repo._data["failed"] = _work("failed", enhancement_error="previous error")

    ids = [w.id for w in repo.list_needing_enhancement()]

    assert ids == ["ok"]


def test_work_needs_enhancement_false_also_excluded() -> None:
    """Regression guard: the added filter must not regress the existing
    `needs_enhancement = FALSE` filter."""
    repo = FakeWorkRepository()
    repo._data["done"] = _work("done", needs_enhancement=False)

    assert repo.list_needing_enhancement() == []


def test_recording_with_enhancement_error_is_excluded_from_queue() -> None:
    repo = FakeRecordingRepository()
    repo._data["ok"] = _recording("ok")
    repo._data["failed"] = _recording(
        "failed", enhancement_error="previous error"
    )

    ids = [r.id for r in repo.list_needing_enhancement()]

    assert ids == ["ok"]


def test_recording_needs_enhancement_false_also_excluded() -> None:
    repo = FakeRecordingRepository()
    repo._data["done"] = _recording("done", needs_enhancement=False)

    assert repo.list_needing_enhancement() == []
