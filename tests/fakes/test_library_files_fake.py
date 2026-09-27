from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.domain.enums import AudioHashKind, EnrichmentStatus, FileStatus
from backend.domain.library import AudioHash, AudioMetadata, LibraryFile
from tests.fakes.library_files import FakeLibraryFileRepository

_UNSET: Any = object()


def _file(
    artist_name: str,
    *,
    normalized_artist_name: str | None = _UNSET,
    recording_mbid: str | None = None,
) -> LibraryFile:
    norm = artist_name.lower() if normalized_artist_name is _UNSET else normalized_artist_name
    return LibraryFile(
        id=uuid4(),
        file_path=f"/music/{artist_name.replace(' ', '_')}-{uuid4()}.mp3",
        file_hash=f"hash-{uuid4()}",
        format="mp3",
        file_status=FileStatus.PRESENT,
        audio=AudioMetadata(
            artist_name=artist_name,
            normalized_artist_name=norm,
            recording_mbid=recording_mbid,
        ),
    )


def test_get_by_normalized_artist_name_exact_match() -> None:
    repo = FakeLibraryFileRepository()
    # Substring overlap (e.g. "the prince of egypt" contains "prince") must
    # NOT match — that is the no-cross-artist invariant.
    for f in [
        _file("Prince", normalized_artist_name="prince"),
        _file("The Prince of Egypt", normalized_artist_name="the prince of egypt"),
        _file("Madonna", normalized_artist_name="madonna"),
    ]:
        repo.upsert(f)
    hits = repo.get_by_normalized_artist_name("prince", limit=10)
    assert {h.audio.artist_name for h in hits} == {"Prince"}


def test_get_by_normalized_artist_name_respects_limit() -> None:
    repo = FakeLibraryFileRepository()
    for _ in range(5):
        repo.upsert(_file("Prince", normalized_artist_name="prince"))
    assert len(repo.get_by_normalized_artist_name("prince", limit=3)) == 3


def test_get_by_normalized_artist_name_no_matches_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Madonna", normalized_artist_name="madonna"))
    assert repo.get_by_normalized_artist_name("prince", limit=10) == []


def test_get_by_normalized_artist_name_empty_input_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Prince", normalized_artist_name="prince"))
    assert repo.get_by_normalized_artist_name("", limit=10) == []


def test_get_by_normalized_artist_name_skips_null_normalized() -> None:
    # A library file whose normalized_artist_name is None must never satisfy
    # an equality query — fail-closed mirrors _filter_to_artist policy.
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Prince", normalized_artist_name=None))
    assert repo.get_by_normalized_artist_name("prince", limit=10) == []


def test_get_by_recording_mbid_hit() -> None:
    repo = FakeLibraryFileRepository()
    f = _file("Prince", recording_mbid="rec-123")
    repo.upsert(f)
    got = repo.get_by_recording_mbid("rec-123")
    assert len(got) == 1
    assert got[0].audio.recording_mbid == "rec-123"


def test_get_by_recording_mbid_miss_returns_empty() -> None:
    repo = FakeLibraryFileRepository()
    assert repo.get_by_recording_mbid("rec-missing") == []


def test_get_by_recording_mbid_returns_all_duplicates() -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_file("Prince", recording_mbid="rec-dup"))
    repo.upsert(_file("Prince", recording_mbid="rec-dup"))
    got = repo.get_by_recording_mbid("rec-dup")
    assert len(got) == 2


def test_upsert_keeps_links_when_incoming_row_has_none() -> None:
    """A fresh tag extraction carries no work/recording link. Re-upserting it
    must not erase links the grouping and enrichment passes already built —
    even when the content hash changed (a retag is not a new song)."""
    repo = FakeLibraryFileRepository()
    original = _file("Prince")
    original.work_id = "work-1"
    original.recording_id = "rec-1"
    repo.upsert(original)

    fresh = LibraryFile(
        id=uuid4(),
        file_path=original.file_path,
        file_hash="retagged",
        format="mp3",
    )
    repo.upsert(fresh)

    got = repo.get_by_path(original.file_path)
    assert got is not None
    assert got.work_id == "work-1"
    assert got.recording_id == "rec-1"


def test_upsert_explicit_links_replace_existing() -> None:
    repo = FakeLibraryFileRepository()
    original = _file("Prince")
    original.work_id = "work-1"
    repo.upsert(original)

    relinked = LibraryFile(
        id=uuid4(),
        file_path=original.file_path,
        file_hash=original.file_hash,
        format="mp3",
        work_id="work-2",
    )
    repo.upsert(relinked)

    got = repo.get_by_path(original.file_path)
    assert got is not None
    assert got.work_id == "work-2"


def test_update_file_stat_records_size_and_mtime() -> None:
    repo = FakeLibraryFileRepository()
    f = _file("Prince")
    repo.upsert(f)

    repo.update_file_stat(f.id, file_size=4096, file_mtime_ns=1_700_000_000_000_000_000)

    got = repo.get_by_id(f.id)
    assert got is not None
    assert got.file_size == 4096
    assert got.file_mtime_ns == 1_700_000_000_000_000_000


def test_get_pending_enrichment_with_release_needs_both_mbids() -> None:
    from backend.domain.enums import EnrichmentStatus

    repo = FakeLibraryFileRepository()
    both = _file("A", recording_mbid="rec-1")
    both = LibraryFile(
        **{**both.__dict__, "audio": AudioMetadata(release_mbid="rel-1", recording_mbid="rec-1")}
    )
    no_release = _file("B", recording_mbid="rec-2")
    enriched = LibraryFile(
        **{
            **_file("C").__dict__,
            "audio": AudioMetadata(release_mbid="rel-1", recording_mbid="rec-3"),
            "enrichment_status": EnrichmentStatus.ENRICHED,
        }
    )
    for f in (both, no_release, enriched):
        repo.upsert(f)

    assert repo.get_pending_enrichment_with_release() == [both]


@pytest.mark.parametrize(
    ("new_size", "new_mtime_ns", "expected"),
    [
        (100, 1_000, EnrichmentStatus.ENRICHED),
        (101, 1_000, EnrichmentStatus.PENDING),
        (100, 2_000, EnrichmentStatus.PENDING),
    ],
)
def test_upsert_over_unhashed_row_mirrors_pg(
    new_size: int,
    new_mtime_ns: int,
    expected: EnrichmentStatus,
) -> None:
    repo = FakeLibraryFileRepository()
    stored = LibraryFile(
        id=uuid4(),
        file_path="/music/a.flac",
        file_hash=None,
        format="flac",
        enrichment_status=EnrichmentStatus.ENRICHED,
        file_size=100,
        file_mtime_ns=1_000,
    )
    repo.upsert(stored)
    got = repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path="/music/a.flac",
            file_hash="h" * 64,
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
            file_size=new_size,
            file_mtime_ns=new_mtime_ns,
        )
    )
    assert got.enrichment_status == expected


def test_upsert_of_two_unhashed_rows_without_stat_resets_enrichment_like_pg() -> None:
    # PG: NULL = NULL is not true, so nothing shows the content is unchanged.
    repo = FakeLibraryFileRepository()
    repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path="/music/a.flac",
            file_hash=None,
            format="flac",
            enrichment_status=EnrichmentStatus.ENRICHED,
        )
    )
    got = repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path="/music/a.flac",
            file_hash=None,
            format="flac",
            enrichment_status=EnrichmentStatus.PENDING,
        )
    )
    assert got.enrichment_status == EnrichmentStatus.PENDING


def _track_file(repo: FakeLibraryFileRepository, path: str, *, missing: bool) -> LibraryFile:
    lf = repo.upsert(
        LibraryFile(
            id=uuid4(),
            file_path=path,
            file_hash=None,
            format="flac",
            audio=AudioMetadata(
                recording_mbid="rec-1",
                normalized_artist_name="artist",
                release_title="Album",
                track_number=1,
                normalized_title="song",
            ),
        )
    )
    if missing:
        repo.mark_missing(path)
    return lf


def test_fake_missing_lookups_mirror_pg() -> None:
    repo = FakeLibraryFileRepository()
    present = _track_file(repo, "/m/new.flac", missing=False)
    gone = _track_file(repo, "/m/old.flac", missing=True)

    assert [f.id for f in repo.get_missing()] == [gone.id]
    assert [f.id for f in repo.get_present_by_track("artist", "Album", 1, "song")] == [present.id]
    assert [f.id for f in repo.get_by_recording_mbid("rec-1")] == [present.id]
    assert [f.id for f in repo.get_by_normalized_artist_name("artist")] == [present.id]
    assert gone.file_status == FileStatus.MISSING


_H1 = AudioHash(AudioHashKind.FLAC_MD5, "1" * 32)
_H2 = AudioHash(AudioHashKind.AUDIO_SHA256, "2" * 64)


def _stat_row(path: str, *, fmt: str = "flac", audio_hash: AudioHash | None = None) -> LibraryFile:
    return LibraryFile(
        id=uuid4(),
        file_path=path,
        file_hash=None,
        format=fmt,
        enrichment_status=EnrichmentStatus.ENRICHED,
        file_size=100,
        file_mtime_ns=1_000,
        audio_hash=audio_hash,
    )


@pytest.mark.parametrize(
    ("size", "incoming", "expected"),
    [(100, None, _H1), (101, None, None), (101, _H2, _H2)],
    ids=["unchanged", "changed", "fresh"],
)
def test_fake_upsert_audio_hash_rule_mirrors_pg(
    size: int, incoming: AudioHash | None, expected: AudioHash | None
) -> None:
    repo = FakeLibraryFileRepository()
    repo.upsert(_stat_row("/m/a.flac", audio_hash=_H1))
    fresh = _stat_row("/m/a.flac", audio_hash=incoming)
    fresh.file_size = size

    assert repo.upsert(fresh).audio_hash == expected


def test_fake_audio_hash_lookups_mirror_pg() -> None:
    repo = FakeLibraryFileRepository()
    hashed = repo.upsert(_stat_row("/m/b.flac", audio_hash=_H1))
    pending = repo.upsert(_stat_row("/m/a.mp3", fmt="mp3"))
    repo.upsert(_stat_row("/m/c.ogg", fmt="ogg"))

    assert repo.get_by_audio_hash(_H1) == [hashed]
    assert [f.file_path for f in repo.get_by_stat(100, 1_000)] == [
        "/m/a.mp3",
        "/m/b.flac",
        "/m/c.ogg",
    ]
    assert repo.get_audio_unhashed_after(None, 10) == [pending]
    assert repo.count_audio_unhashed() == 1
    assert repo.set_audio_hash(pending.id, _H2, 100, 2_000) is False
    assert repo.set_audio_hash(pending.id, _H2, 100, 1_000) is True
    assert repo.count_audio_unhashed() == 0
