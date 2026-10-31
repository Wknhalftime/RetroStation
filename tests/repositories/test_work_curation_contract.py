"""The work-curation port's fake against its PostgreSQL adapter (artist model phase 0c, S3).

Each case seeds rows through the shared repository ports, runs port methods, and reads the
result back through the ports. It runs on the in-memory fakes (``tests/fakes/
work_curation.py`` over the other fakes' rows) and on PostgreSQL, so the fake cannot drift
from the SQL. The service's own behaviour is pinned in
``tests/services/test_work_curation_service.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.catalog import Recording, RecordingVersion
from backend.domain.curation import FormatOverride, SongMaster
from backend.domain.enums import (
    MatchTier,
    ReleaseStatus,
    ReleaseType,
    SelectionMethod,
    TargetType,
    VersionType,
)
from backend.domain.library import AudioMetadata, LibraryFile
from backend.domain.matching import Match
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.work_curation import WorkCurationRepository
from backend.repositories.works import WorkRepository
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.artists import FakeArtistRepository
from tests.fakes.format_overrides import FakeFormatOverrideRepository
from tests.fakes.library_files import FakeLibraryFileRepository
from tests.fakes.matches import FakeMatchRepository
from tests.fakes.recordings import FakeRecordingRepository
from tests.fakes.song_masters import FakeSongMasterRepository
from tests.fakes.work_curation import FakeWorkCurationRepository
from tests.fakes.works import FakeWorkRepository

EARLY = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Ports:
    curation: WorkCurationRepository
    works: WorkRepository
    files: LibraryFileRepository
    recordings: RecordingRepository
    song_masters: SongMasterRepository
    overrides: FormatOverrideRepository
    matches: MatchRepository
    artists: ArtistCatalogRepository
    add_match: Callable[[Match], object]
    """Store a match (PostgreSQL first stores the line or name its FK names)."""
    set_created_at: Callable[[UUID, datetime], None]
    """Date an override (the PostgreSQL create leaves created_at to now())."""


def _fake_ports() -> Ports:
    works, files, recordings = (
        FakeWorkRepository(),
        FakeLibraryFileRepository(),
        FakeRecordingRepository(),
    )
    masters, overrides, matches = (
        FakeSongMasterRepository(),
        FakeFormatOverrideRepository(),
        FakeMatchRepository(),
    )
    artists = FakeArtistRepository()
    works.set_library_file_repo(files)
    masters.set_library_file_repo(files)

    def set_created_at(override_id: UUID, when: datetime) -> None:
        overrides._data[override_id].created_at = when

    return Ports(
        curation=FakeWorkCurationRepository(
            works=works,
            files=files,
            recordings=recordings,
            song_masters=masters,
            overrides=overrides,
            matches=matches,
            artists=artists,
        ),
        works=works,
        files=files,
        recordings=recordings,
        song_masters=masters,
        overrides=overrides,
        matches=matches,
        artists=artists,
        add_match=matches.create,
        set_created_at=set_created_at,
    )


def _pg_ports(conn: psycopg.Connection[dict[str, object]]) -> Ports:
    factory = RepositoryFactory(conn)

    def add_match(match: Match) -> None:
        name_id = match.artist_id or uuid4()
        conn.execute(
            "INSERT INTO broadcast_artists (id, original_name, normalized_name)"
            " VALUES (%s, %s, %s)",
            (name_id, "NAME", str(name_id)),
        )
        if match.identity_id is not None:
            conn.execute(
                "INSERT INTO track_identities (id, broadcast_artist_id, original_title,"
                " normalized_title, normalized_signature) VALUES (%s, %s, 'SONG', 'song', %s)",
                (match.identity_id, name_id, str(match.identity_id)),
            )
        factory.matches.create(match)

    def set_created_at(override_id: UUID, when: datetime) -> None:
        conn.execute(
            "UPDATE format_overrides SET created_at = %s WHERE id = %s", (when, override_id)
        )

    return Ports(
        curation=factory.work_curation,
        works=factory.works,
        files=factory.library_files,
        recordings=factory.recordings,
        song_masters=factory.song_masters,
        overrides=factory.format_overrides,
        matches=factory.matches,
        artists=factory.artists,
        add_match=add_match,
        set_created_at=set_created_at,
    )


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def ports(request: pytest.FixtureRequest) -> Iterator[Ports]:
    if request.param == "fake":
        yield _fake_ports()
        return
    migrated_db: str = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield _pg_ports(conn)
        conn.rollback()


# --- seeds and reads ---------------------------------------------------------------------------


def _work(ports: Ports, title: str = "Lithium", artist: str = "Nirvana") -> str:
    return ports.works.create_local(title, ports.artists.upsert_local_artist(artist, artist))


def _file(
    ports: Ports,
    work_id: str | None,
    name: str,
    *,
    fmt: str = "mp3",
    recording_id: str | None = None,
    missing: bool = False,
    bitrate: int | None = None,
    release: tuple[ReleaseStatus, ReleaseType] | None = None,
) -> UUID:
    path = rf"D:\music\{name}.{fmt}"
    status, kind = release if release is not None else (None, None)
    file = LibraryFile(
        id=uuid4(),
        file_path=path,
        format=fmt,
        work_id=work_id,
        recording_id=recording_id,
        audio=AudioMetadata(
            bitrate=bitrate, duration_ms=1000, release_status=status, release_type=kind
        ),
    )
    file_id = ports.files.upsert(file).id
    if missing:
        ports.files.mark_missing(path)
    return file_id


def _recording(ports: Ports, work_id: str, version: VersionType = VersionType.ORIGINAL) -> str:
    recording = Recording(id=str(uuid4()), title="take", work_id=work_id, version_type=version)
    return ports.recordings.upsert(recording).id


def _override(ports: Ports, work_id: str, fmt: str, file_id: UUID, when: datetime) -> UUID:
    override = FormatOverride(
        id=uuid4(), work_id=work_id, format_name=fmt, preferred_file_id=file_id
    )
    override_id = ports.overrides.create(override).id
    ports.set_created_at(override_id, when)
    return override_id


def _line(
    ports: Ports,
    *,
    file_id: UUID | None = None,
    work_id: str | None = None,
    target: str | None = None,
) -> UUID:
    """A song line's match (to a file, or to a work as its target); its identity id."""
    identity = uuid4()
    ports.add_match(
        Match(
            id=uuid4(),
            confidence_score=1.0,
            match_tier=MatchTier.MANUAL,
            identity_id=identity,
            library_file_id=file_id,
            target_id=target,
            target_type=TargetType.WORK if target is not None else TargetType.LIBRARY_FILE,
            work_id=work_id,
        )
    )
    return identity


def _work_of(ports: Ports, file_id: UUID) -> str | None:
    file = ports.files.get_by_id(file_id)
    assert file is not None
    return file.work_id


def _recording_of(ports: Ports, file_id: UUID) -> str | None:
    file = ports.files.get_by_id(file_id)
    assert file is not None
    return file.recording_id


def _match(ports: Ports, identity: UUID) -> Match:
    match = ports.matches.get_by_identity(identity)
    assert match is not None
    return match


def _master(ports: Ports, work_id: str) -> tuple[UUID, str, int | None] | None:
    master = ports.song_masters.get_by_work(work_id)
    if master is None:
        return None
    return master.preferred_file_id, master.selection_method.value, master.score


# --- merge's statements --------------------------------------------------------------------------


def test_files_and_overrides_move(ports: Ports) -> None:
    target, first, second = _work(ports), _work(ports, "a"), _work(ports, "b")
    kept, a, b = _file(ports, target, "kept"), _file(ports, first, "a"), _file(ports, second, "b")
    gone = _file(ports, second, "gone", missing=True)
    _override(ports, target, "flac", kept, EARLY)
    _override(ports, first, "flac", a, EARLY)  # the target's own wins
    _override(ports, first, "mp3", a, EARLY + timedelta(days=1))  # the earlier source's wins
    winner = _override(ports, second, "mp3", b, EARLY)
    _override(ports, second, "aac", b, EARLY)

    assert ports.curation.move_files_of_works([first, second], target) == 3
    assert ports.curation.drop_colliding_overrides([first, second], target) == 2
    ports.curation.move_overrides_of_works([first, second], target)

    assert {f: _work_of(ports, f) for f in (a, b, gone)} == dict.fromkeys((a, b, gone), target)
    stored = {
        o.format_name: (o.preferred_file_id, o.id) for o in ports.overrides.list_by_work(target)
    }
    assert {name: file for name, (file, _) in stored.items()} == {"flac": kept, "mp3": b, "aac": b}
    assert stored["mp3"][1] == winner
    assert ports.overrides.list_by_work(first) == ports.overrides.list_by_work(second) == []


def test_recordings_are_listed_moved_and_folded(ports: Ports) -> None:
    target, source = _work(ports), _work(ports, "source")
    original, live = _recording(ports, source), _recording(ports, source, VersionType.LIVE)
    kept_take = _recording(ports, target)
    on_original = _file(ports, source, "original", recording_id=original)

    listed = sorted(ports.curation.recordings_of_works([source]), key=lambda r: r.version_type)
    assert listed == sorted(
        [
            RecordingVersion(original, VersionType.ORIGINAL),
            RecordingVersion(live, VersionType.LIVE),
        ],
        key=lambda r: r.version_type,
    )
    ports.curation.move_recording_files(original, kept_take)
    ports.curation.delete_recording(original)
    ports.curation.move_recording(live, target)

    assert _recording_of(ports, on_original) == kept_take
    assert ports.recordings.get_by_id(original) is None
    moved = ports.recordings.get_by_id(live)
    assert moved is not None and moved.work_id == target
    assert ports.curation.recordings_of_works([]) == []


def test_matches_follow_and_works_and_artists_go(ports: Ports) -> None:
    target, source = _work(ports), _work(ports, "source", "Solo")
    file_id = _file(ports, source, "song")
    on_file = _line(ports, file_id=file_id, work_id=source)
    on_work = _line(ports, target=source)
    elsewhere = _line(ports, target=target)

    ports.curation.move_matches_of_works([source], target)
    assert (_match(ports, on_file).work_id, _match(ports, on_work).target_id) == (target, target)
    assert _match(ports, elsewhere).target_id == target

    ports.curation.move_files_of_works([source], target)
    assert ports.curation.delete_works([source, source, "no-such-work"]) == 1
    assert ports.works.get_by_id(source) is None

    solo = ports.artists.upsert_local_artist("Solo", "Solo")
    named = ports.artists.upsert_local_artist("Named", "Named")
    ports.add_match(
        Match(
            id=uuid4(),
            confidence_score=1.0,
            match_tier=MatchTier.MANUAL,
            artist_id=uuid4(),
            target_id=named,
            target_type=TargetType.ARTIST,
        )
    )
    assert (ports.curation.artist_has_match(solo), ports.curation.artist_has_match(named)) == (
        False,
        True,
    )
    ports.curation.delete_artist(solo)
    assert ports.artists.get_by_id(solo) is None


# --- split's and reassign's statements --------------------------------------------------------


def test_recording_copy_file_recording_and_split_master(ports: Ports) -> None:
    work = _work(ports)
    take = _recording(ports, work)
    one, two = (
        _file(ports, work, "one", recording_id=take),
        _file(ports, work, "two", recording_id=take),
    )
    copy = Recording(
        id=str(uuid4()),
        title="take",
        work_id=work,
        version_type=VersionType.LIVE,
        duration_ms=1234,
        needs_enhancement=True,
    )

    assert ports.curation.count_other_files_on_recording(take, one) == 1
    ports.curation.insert_recording(copy)
    ports.curation.set_file_recording(two, copy.id)
    assert ports.curation.count_other_files_on_recording(take, one) == 0
    master_id = uuid4()
    ports.curation.store_split_master(master_id, work, two)

    stored = ports.recordings.get_by_id(copy.id)
    assert stored is not None
    assert (stored.work_id, stored.version_type, stored.duration_ms, stored.needs_enhancement) == (
        work,
        VersionType.LIVE,
        1234,
        False,
    )
    assert _recording_of(ports, two) == copy.id
    master = ports.song_masters.get_by_work(work)
    assert master is not None and master.id == master_id
    assert _master(ports, work) == (two, "auto", None)


def test_a_left_work_is_cleared(ports: Ports) -> None:
    left, other = _work(ports), _work(ports, "other")
    moved = _file(ports, other, "moved")
    on_moved = _line(ports, file_id=moved, work_id=left)  # left behind on the old work
    _override(ports, left, "mp3", _file(ports, left, "stays"), EARLY)
    take = _recording(ports, left)

    ports.curation.release_work_matches(left)
    ports.curation.delete_overrides_of_work(left)
    ports.curation.delete_recordings_of_work(left)

    assert _match(ports, on_moved).work_id == other
    assert ports.overrides.list_by_work(left) == []
    assert ports.recordings.get_by_id(take) is None


# --- the master recalculation's statements -------------------------------------------------------


def test_master_candidates_and_writes(ports: Ports) -> None:
    work, other = _work(ports), _work(ports, "other")
    best = _file(ports, work, "best", fmt="flac", bitrate=900)
    plain = _file(ports, work, "plain")
    _file(ports, work, "gone", missing=True)
    elsewhere = _file(ports, other, "elsewhere")

    candidates = sorted(
        ports.curation.present_master_candidates(work), key=lambda c: c.format or ""
    )
    assert [(c.id, c.format, c.bitrate, c.duration_ms) for c in candidates] == [
        (best, "flac", 900, 1000),
        (plain, "mp3", None, 1000),
    ]

    picked = uuid4()
    ports.song_masters.upsert(
        SongMaster(
            id=picked,
            work_id=work,
            preferred_file_id=plain,
            selection_method=SelectionMethod.MANUAL,
        )
    )
    ports.curation.store_auto_master(uuid4(), work, best, 10)  # replaces even a manual pick
    stored = ports.song_masters.get_by_work(work)
    assert stored is not None and stored.id == picked
    assert _master(ports, work) == (best, "auto", 10)

    ports.curation.delete_master_off_work(work)  # on the work's own file: stays
    assert _master(ports, work) == (best, "auto", 10)
    ports.curation.store_auto_master(uuid4(), work, elsewhere, 3)
    ports.curation.delete_master_off_work(work)  # on another work's file: goes
    assert _master(ports, work) is None


def test_an_override_is_stored_once_per_format(ports: Ports) -> None:
    work = _work(ports)
    file_id = _file(ports, work, "song")

    def override(fmt: str) -> FormatOverride:
        return FormatOverride(
            id=uuid4(), work_id=work, format_name=fmt, preferred_file_id=file_id, notes="n"
        )

    assert ports.curation.insert_override_if_absent(override("flac")) is True
    assert ports.curation.insert_override_if_absent(override("flac")) is False
    assert ports.curation.insert_override_if_absent(override("mp3")) is True
    assert sorted(o.format_name for o in ports.overrides.list_by_work(work)) == ["flac", "mp3"]


# --- where the fake could drift: tie-breaks, empty cases, stored text --------------------------


def test_edge_cases_the_fake_and_the_sql_agree_on(ports: Ports) -> None:
    target, first, second = _work(ports), _work(ports, "a"), _work(ports, "b")
    a, b = _file(ports, first, "a"), _file(ports, second, "b")
    tied = sorted(
        [_override(ports, first, "ogg", a, EARLY), _override(ports, second, "ogg", b, EARLY)]
    )

    # Two sources' overrides of one format at the same created_at: the lower id wins.
    assert ports.curation.drop_colliding_overrides([first, second], target) == 1
    left = ports.overrides.list_by_work(first) + ports.overrides.list_by_work(second)
    assert [o.id for o in left] == tied[:1]

    # The sources' masters go whatever their method; a work with no master takes the new id.
    ports.song_masters.upsert(
        SongMaster(
            id=uuid4(), work_id=first, preferred_file_id=a, selection_method=SelectionMethod.MANUAL
        )
    )
    ports.curation.delete_masters_of_works([first, second])
    assert ports.song_masters.get_by_work(first) is None
    fresh = uuid4()
    ports.curation.store_auto_master(fresh, second, b, 3)
    stored = ports.song_masters.get_by_work(second)
    assert stored is not None and stored.id == fresh
    assert _master(ports, second) == (b, "auto", 3)

    # A match with no file, released from its work, is left with none.
    no_file = _line(ports, work_id=first, target=first)
    ports.curation.release_work_matches(first)
    assert _match(ports, no_file).work_id is None

    # A candidate carries the file's stored release status and type.
    tagged = _file(ports, target, "tagged", release=(ReleaseStatus.PROMOTION, ReleaseType.ALBUM))
    [candidate] = ports.curation.present_master_candidates(target)
    assert (candidate.id, candidate.release_status, candidate.release_type) == (
        tagged,
        "promotion",
        "album",
    )
