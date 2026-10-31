"""The works router's writes as one sync service (artist model phase 0c, SPEC S3, E2, E3; C75).

``work_curation_service`` holds the writes the works router ran as raw SQL on its async
connection: merge, split, reassign, format override create and delete, master delete, and the
master recalculation they share. It writes in the caller's transaction and reproduces today's
behaviour, oddities included: a merge drops a source's hand-picked master, and the
recalculation replaces a manual pick whose file is no longer a present file of the work
(phase 3b changes both). Each refusal carries today's HTTP detail as its message.

The routes' own tests (``tests/routers/test_library.py``, unlocked) and phase 0a's scenario
tests are the safety net for every response; these pin the service: its results, its
refusals, and its transaction. E2: a second merge of the same source waits on the first's
row lock, then finds nothing left to move. E3: a write that fails halfway leaves nothing.

Seeds hold no artist rows in ``matches`` (phase 1c drops them), only song lines.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.domain.broadcast import BroadcastArtist, BroadcastTrackIdentity
from backend.domain.catalog import Recording
from backend.domain.curation import (
    CurationFileMissingError,
    CurationFileNotFoundError,
    CurationFileNotOnWorkError,
    CurationWorkNotFoundError,
    FileAlreadyOnWorkError,
    FormatOverride,
    FormatOverrideExistsError,
    FormatOverrideNotFoundError,
    MergeResult,
    MergeTargetInSourcesError,
    NewFormatOverride,
    ReassignResult,
    SongMaster,
    SongMasterNotFoundError,
)
from backend.domain.enums import SelectionMethod, VersionType
from backend.domain.library import LibraryFile
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.work_curation_service import (
    WorkCurationRepos,
    create_format_override,
    delete_format_override,
    delete_master,
    merge_works,
    reassign_file,
    split_file_off,
)

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

EARLY = datetime(2026, 10, 1, tzinfo=UTC)
NOTHING_MERGED = MergeResult(merged_file_count=0, deleted_work_count=0, dropped_override_count=0)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def _repos(conn: Conn) -> WorkCurationRepos:
    return work_curation_repos(RepositoryFactory(conn))


# --- seeds -----------------------------------------------------------------------------------


def _artist(conn: Conn, name: str) -> str:
    return RepositoryFactory(conn).artists.upsert_local_artist(name, name.lower())


def _work(conn: Conn, title: str, artist_id: str) -> str:
    return RepositoryFactory(conn).works.create_local(title, artist_id)


def _recording(
    conn: Conn,
    work_id: str,
    version: VersionType = VersionType.ORIGINAL,
    duration_ms: int | None = None,
) -> str:
    recording = Recording(
        id=str(uuid4()),
        title=f"take {version.value}",
        work_id=work_id,
        version_type=version,
        duration_ms=duration_ms,
    )
    return RepositoryFactory(conn).recordings.upsert(recording).id


def _file(
    conn: Conn,
    work_id: str | None,
    name: str,
    *,
    fmt: str = "mp3",
    recording_id: str | None = None,
    missing: bool = False,
) -> UUID:
    file = LibraryFile(
        id=uuid4(),
        file_path=rf"D:\music\{name}.{fmt}",
        format=fmt,
        work_id=work_id,
        recording_id=recording_id,
    )
    file_id = RepositoryFactory(conn).library_files.upsert(file).id
    if missing:
        conn.execute("UPDATE library_files SET file_status = 'missing' WHERE id = %s", (file_id,))
    return file_id


def _override(conn: Conn, work_id: str, fmt: str, file_id: UUID, created_at: datetime) -> UUID:
    """An override created at *created_at* (the repository's create leaves it to now())."""
    override = FormatOverride(
        id=uuid4(), work_id=work_id, format_name=fmt, preferred_file_id=file_id
    )
    override_id = RepositoryFactory(conn).format_overrides.create(override).id
    conn.execute(
        "UPDATE format_overrides SET created_at = %s WHERE id = %s", (created_at, override_id)
    )
    return override_id


def _master(conn: Conn, work_id: str, file_id: UUID, method: SelectionMethod) -> None:
    master = SongMaster(
        id=uuid4(), work_id=work_id, preferred_file_id=file_id, selection_method=method
    )
    RepositoryFactory(conn).song_masters.upsert(master)


def _line_match(conn: Conn, file_id: UUID, work_id: str) -> UUID:
    """A song line's match to *file_id*, carrying the file's work."""
    factory = RepositoryFactory(conn)
    artist = factory.broadcast_artists.upsert(
        BroadcastArtist(id=uuid4(), original_name="NIRVANA", normalized_name=str(uuid4()))
    )
    identity = factory.broadcast_identities.upsert(
        BroadcastTrackIdentity(
            id=uuid4(),
            broadcast_artist_id=artist.id,
            original_title="LITHIUM",
            normalized_title="lithium",
            normalized_signature=str(uuid4()),
        )
    )
    row = conn.execute(
        """INSERT INTO matches (identity_id, library_file_id, target_type, work_id,
                                confidence_score, match_tier)
           VALUES (%s, %s, 'library_file', %s, 1.0, 'manual') RETURNING id""",
        (identity.id, file_id, work_id),
    ).fetchone()
    assert row is not None
    return UUID(str(row["id"]))


# --- reads -----------------------------------------------------------------------------------


def _row(conn: Conn, table: str, row_id: object) -> DictRow | None:
    assert table in {"works", "recordings", "artists"}
    return conn.execute(f"SELECT * FROM {table} WHERE id = %s", (row_id,)).fetchone()


def _work_of(conn: Conn, file_id: UUID) -> str | None:
    row = conn.execute("SELECT work_id FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None
    return row["work_id"]


def _recording_of(conn: Conn, file_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT recording_id FROM library_files WHERE id = %s", (file_id,)
    ).fetchone()
    assert row is not None
    return row["recording_id"]


def _master_of(conn: Conn, work_id: str) -> tuple[UUID, str] | None:
    row = conn.execute(
        "SELECT preferred_file_id, selection_method FROM song_masters WHERE work_id = %s",
        (work_id,),
    ).fetchone()
    return None if row is None else (row["preferred_file_id"], row["selection_method"])


def _overrides_of(conn: Conn, work_id: str) -> dict[str, UUID]:
    rows = conn.execute(
        "SELECT format_name, preferred_file_id FROM format_overrides WHERE work_id = %s",
        (work_id,),
    ).fetchall()
    return {r["format_name"]: r["preferred_file_id"] for r in rows}


def _match_work(conn: Conn, match_id: UUID) -> str | None:
    row = conn.execute("SELECT work_id FROM matches WHERE id = %s", (match_id,)).fetchone()
    assert row is not None
    return row["work_id"]


# --- S3: merge -------------------------------------------------------------------------------


def test_merge_folds_every_source_into_the_target(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    solo = _artist(conn, "Kurt Solo")  # local, and its only work merges away: deleted
    target = _work(conn, "Lithium", artist)
    take = _recording(conn, target)
    best = _file(conn, target, "best", fmt="flac", recording_id=take)
    first = _work(conn, "Lithium (solo)", solo)
    same_take = _recording(conn, first)  # the target has this version: its file joins `take`
    live_take = _recording(conn, first, VersionType.LIVE)  # the target lacks it: it moves
    solo_file = _file(conn, first, "solo", recording_id=same_take)
    solo_live = _file(conn, first, "solo-live", recording_id=live_take)
    second = _work(conn, "lithium", artist)
    other = _file(conn, second, "other")
    gone = _file(conn, second, "gone", missing=True)  # moved and counted like any other file
    _override(conn, target, "flac", best, EARLY)
    _override(conn, first, "flac", solo_file, EARLY)  # the target's own wins: dropped
    _override(conn, first, "mp3", solo_file, EARLY + timedelta(days=1))  # the older wins: dropped
    _override(conn, second, "mp3", other, EARLY)  # moves
    line = _line_match(conn, solo_file, first)
    conn.commit()

    result = merge_works(target, [first, second, "no-such-work"], _repos(conn))
    conn.commit()

    assert result == MergeResult(
        merged_file_count=4, deleted_work_count=2, dropped_override_count=2
    )
    files = (best, solo_file, solo_live, other, gone)
    assert {f: _work_of(conn, f) for f in files} == dict.fromkeys(files, target)
    assert _recording_of(conn, solo_file) == take
    assert _row(conn, "recordings", same_take) is None
    moved_take = _row(conn, "recordings", live_take)
    assert moved_take is not None and moved_take["work_id"] == target
    assert _overrides_of(conn, target) == {"flac": best, "mp3": other}
    assert (_row(conn, "works", first), _row(conn, "works", second)) == (None, None)
    assert _master_of(conn, first) is None
    assert _master_of(conn, target) == (best, "auto")
    assert _match_work(conn, line) == target
    assert _row(conn, "artists", solo) is None
    assert _row(conn, "artists", artist) is not None


def test_merge_drops_a_sources_hand_picked_master(conn: Conn) -> None:
    """Today's oddity, kept: the source's manual pick is not carried (phase 3b changes it)."""
    artist = _artist(conn, "Nirvana")
    target = _work(conn, "Lithium", artist)
    best = _file(conn, target, "best", fmt="flac")
    source = _work(conn, "lithium", artist)
    picked = _file(conn, source, "picked")
    _master(conn, source, picked, SelectionMethod.MANUAL)
    conn.commit()

    merge_works(target, [source], _repos(conn))
    conn.commit()

    assert _master_of(conn, source) is None
    assert _master_of(conn, target) == (best, "auto")


def test_merge_keeps_a_source_artist_with_another_work_or_from_musicbrainz(conn: Conn) -> None:
    target = _work(conn, "Lithium", _artist(conn, "Nirvana"))
    busy = _artist(conn, "Busy")
    _work(conn, "Another Song", busy)
    from_busy = _work(conn, "Lithium", busy)
    catalogued = str(uuid4())
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin)
           VALUES (%s, 'Catalogued', 'Catalogued', 'catalogued', %s, 'musicbrainz')""",
        (catalogued, str(uuid4())),
    )
    from_catalogued = _work(conn, "Lithium", catalogued)
    conn.commit()

    result = merge_works(target, [from_busy, from_catalogued], _repos(conn))
    conn.commit()

    assert result == MergeResult(
        merged_file_count=0, deleted_work_count=2, dropped_override_count=0
    )
    assert _row(conn, "artists", busy) is not None
    assert _row(conn, "artists", catalogued) is not None


def test_merge_refusals_and_edge_cases(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    target = _work(conn, "Lithium", artist)
    twice = _work(conn, "lithium", artist)
    _file(conn, twice, "twice")
    conn.commit()
    repos = _repos(conn)

    with pytest.raises(CurationWorkNotFoundError, match=r"^Work no-such-work not found$"):
        merge_works("no-such-work", [target], repos)
    with pytest.raises(CurationWorkNotFoundError, match=r"^Work no-such-work not found$"):
        merge_works("no-such-work", ["no-such-work"], repos)  # the target is checked first
    with pytest.raises(
        MergeTargetInSourcesError, match=r"^Target work ID must not appear in source_work_ids$"
    ):
        merge_works(target, [target], repos)
    assert merge_works(target, [], repos) == NOTHING_MERGED
    assert merge_works(target, ["no-such-work"], repos) == NOTHING_MERGED
    assert merge_works(target, [twice, twice], repos) == MergeResult(
        merged_file_count=1, deleted_work_count=1, dropped_override_count=0
    )


# --- S3: split -------------------------------------------------------------------------------


def test_split_takes_a_file_to_a_work_of_its_own(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    work = _work(conn, "Lithium", artist)
    take = _recording(conn, work, VersionType.LIVE, duration_ms=257_000)
    studio = _file(conn, work, "studio", fmt="flac", recording_id=take)
    live = _file(conn, work, "live", recording_id=take)
    line = _line_match(conn, live, work)
    conn.commit()

    result = split_file_off(work, live, _repos(conn))
    conn.commit()

    assert result.old_work_deleted is False
    new = _row(conn, "works", result.new_work_id)
    assert new is not None
    assert (new["title"], new["artist_id"], new["origin"], new["needs_enhancement"]) == (
        "Lithium",
        artist,
        "local",
        False,
    )
    copy = _recording_of(conn, live)
    assert copy not in (take, None)
    copied = _row(conn, "recordings", copy)
    assert copied is not None
    assert (copied["work_id"], copied["title"], copied["version_type"], copied["duration_ms"]) == (
        result.new_work_id,
        "take live",
        "live",
        257_000,
    )
    assert _recording_of(conn, studio) == take
    assert _work_of(conn, live) == result.new_work_id
    assert _match_work(conn, line) == result.new_work_id
    split_master = conn.execute(
        "SELECT preferred_file_id, selection_method, score FROM song_masters WHERE work_id = %s",
        (result.new_work_id,),
    ).fetchone()
    assert split_master is not None
    assert (split_master["preferred_file_id"], split_master["selection_method"]) == (live, "auto")
    assert split_master["score"] is None
    assert _master_of(conn, work) == (studio, "auto")


def test_split_of_a_works_last_file_deletes_the_work(conn: Conn) -> None:
    work = _work(conn, "Lithium", _artist(conn, "Nirvana"))
    take = _recording(conn, work)
    only = _file(conn, work, "only", recording_id=take)
    _override(conn, work, "mp3", only, EARLY)
    _master(conn, work, only, SelectionMethod.MANUAL)
    conn.commit()

    result = split_file_off(work, only, _repos(conn))
    conn.commit()

    assert result.old_work_deleted is True
    assert _row(conn, "works", work) is None
    assert _row(conn, "recordings", take) is None
    assert (_master_of(conn, work), _overrides_of(conn, work)) == (None, {})
    assert _work_of(conn, only) == result.new_work_id


def test_split_refusals(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    work = _work(conn, "Lithium", artist)
    elsewhere = _file(conn, _work(conn, "Breed", artist), "breed")
    conn.commit()
    repos = _repos(conn)

    with pytest.raises(CurationWorkNotFoundError, match=r"^Work no-such-work not found$"):
        split_file_off("no-such-work", elsewhere, repos)
    with pytest.raises(
        CurationFileNotOnWorkError, match=rf"^File {elsewhere} does not belong to work {work}$"
    ):
        split_file_off(work, elsewhere, repos)


# --- S3: reassign ----------------------------------------------------------------------------


def test_reassign_repoints_the_files_recording(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    home = _work(conn, "Lithium", artist)
    away = _work(conn, "Lithium (other)", artist)
    away_take = _recording(conn, away)
    home_take = _recording(conn, home)
    shared = _recording(conn, home, VersionType.LIVE, duration_ms=1000)
    sole = _recording(conn, home, VersionType.ACOUSTIC)
    # away has the version: joins it, though home_take is shared too (the target's comes first)
    joins = _file(conn, home, "joins", recording_id=home_take)
    _file(conn, home, "joins-sibling", recording_id=home_take)
    copies = _file(conn, home, "copies", recording_id=shared)  # shared: a copy goes to away
    stays = _file(conn, home, "stays", fmt="flac", recording_id=shared)
    moves = _file(conn, home, "moves", fmt="flac", recording_id=sole)  # its own: it moves
    conn.commit()
    repos = _repos(conn)

    for file_id in (joins, copies, moves):
        assert reassign_file(file_id, away, repos) == ReassignResult(
            old_work_id=home, old_work_deleted=False
        )
    conn.commit()

    assert {f: _work_of(conn, f) for f in (joins, copies, moves)} == dict.fromkeys(
        (joins, copies, moves), away
    )
    assert _recording_of(conn, joins) == away_take
    copy = _recording_of(conn, copies)
    assert copy not in (shared, None)
    copied = _row(conn, "recordings", copy)
    assert copied is not None
    assert (copied["work_id"], copied["title"], copied["version_type"], copied["duration_ms"]) == (
        away,
        "take live",
        "live",
        1000,
    )
    kept = _row(conn, "recordings", shared)
    assert kept is not None and kept["work_id"] == home
    assert _recording_of(conn, stays) == shared
    moved = _row(conn, "recordings", sole)
    assert _recording_of(conn, moves) == sole and moved is not None and moved["work_id"] == away
    assert _master_of(conn, away) == (moves, "auto")
    assert _master_of(conn, home) == (stays, "auto")


def test_reassign_deletes_the_emptied_work_and_takes_a_file_with_no_work(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    old = _work(conn, "Lithium", artist)
    target = _work(conn, "Lithium (other)", artist)
    last = _file(conn, old, "last")
    _master(conn, old, last, SelectionMethod.AUTO)
    line = _line_match(conn, last, old)
    loose = _file(conn, None, "loose")
    conn.commit()
    repos = _repos(conn)

    assert reassign_file(last, target, repos) == ReassignResult(
        old_work_id=old, old_work_deleted=True
    )
    assert reassign_file(loose, target, repos) == ReassignResult(
        old_work_id=None, old_work_deleted=False
    )
    conn.commit()

    assert _row(conn, "works", old) is None
    assert _master_of(conn, old) is None
    assert _match_work(conn, line) == target
    assert (_work_of(conn, last), _work_of(conn, loose)) == (target, target)


def test_reassign_refusals(conn: Conn) -> None:
    work = _work(conn, "Lithium", _artist(conn, "Nirvana"))
    file_id = _file(conn, work, "lithium")
    conn.commit()
    repos = _repos(conn)
    nowhere = uuid4()

    with pytest.raises(CurationFileNotFoundError, match=rf"^File {nowhere} not found$"):
        reassign_file(nowhere, work, repos)
    with pytest.raises(CurationFileNotFoundError, match=rf"^File {nowhere} not found$"):
        reassign_file(nowhere, "no-such-work", repos)  # the file is checked first
    with pytest.raises(CurationWorkNotFoundError, match=r"^Work no-such-work not found$"):
        reassign_file(file_id, "no-such-work", repos)
    with pytest.raises(
        FileAlreadyOnWorkError, match=rf"^File {file_id} is already assigned to work {work}$"
    ):
        reassign_file(file_id, work, repos)


# --- S3: the master recalculation the writes share --------------------------------------------


def test_recalculation_keeps_a_valid_manual_pick_and_replaces_an_invalid_one(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    source = _work(conn, "Source", artist)
    kept = _work(conn, "Kept", artist)
    picked = _file(conn, kept, "picked")
    _file(conn, kept, "better", fmt="flac")
    _master(conn, kept, picked, SelectionMethod.MANUAL)  # a present file of the work: kept
    replaced = _work(conn, "Replaced", artist)
    gone = _file(conn, replaced, "gone", fmt="flac", missing=True)
    best = _file(conn, replaced, "best", fmt="flac")
    _master(conn, replaced, gone, SelectionMethod.MANUAL)  # missing: the auto pick replaces it
    into_kept, into_replaced = _file(conn, source, "a"), _file(conn, source, "b")
    conn.commit()
    repos = _repos(conn)

    reassign_file(into_kept, kept, repos)
    reassign_file(into_replaced, replaced, repos)
    conn.commit()

    assert _master_of(conn, kept) == (picked, "manual")
    assert _master_of(conn, replaced) == (best, "auto")


def test_recalculation_with_no_present_file_keeps_only_a_master_on_the_work(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    elsewhere = _work(conn, "Elsewhere", artist)
    left = _work(conn, "Left", artist)
    _file(conn, left, "left-missing", missing=True)
    leaves = _file(conn, left, "leaves")
    _master(conn, left, leaves, SelectionMethod.AUTO)  # follows its file away: removed
    own = _work(conn, "Own", artist)
    own_missing = _file(conn, own, "own-missing", missing=True)
    own_present = _file(conn, own, "own-present")
    _master(conn, own, own_missing, SelectionMethod.AUTO)  # on the work's own file: kept
    conn.commit()
    repos = _repos(conn)

    assert reassign_file(leaves, elsewhere, repos).old_work_deleted is False
    assert reassign_file(own_present, elsewhere, repos).old_work_deleted is False
    conn.commit()

    assert _master_of(conn, left) is None
    assert _master_of(conn, own) == (own_missing, "auto")


# --- S3: format overrides and the master delete ------------------------------------------------


def test_format_override_create_and_delete(conn: Conn) -> None:
    artist = _artist(conn, "Nirvana")
    work = _work(conn, "Lithium", artist)
    other_work = _work(conn, "Breed", artist)
    chosen = _file(conn, work, "chosen", fmt="flac")
    missing = _file(conn, work, "missing", missing=True)
    foreign = _file(conn, other_work, "foreign")
    conn.commit()
    repos = _repos(conn)
    overrides = RepositoryFactory(conn).format_overrides

    def ask(work_id: str, file_id: UUID) -> FormatOverride:
        new = NewFormatOverride(format_name="flac", preferred_file_id=file_id, notes="the box set")
        return create_format_override(work_id, new, repos)

    created = ask(work, chosen)
    conn.commit()

    assert (created.work_id, created.format_name, created.preferred_file_id, created.notes) == (
        work,
        "flac",
        chosen,
        "the box set",
    )
    assert _overrides_of(conn, work) == {"flac": chosen}
    with pytest.raises(
        FormatOverrideExistsError,
        match=rf"^Format override for work {work} and format 'flac' already exists$",
    ):
        ask(work, chosen)
    with pytest.raises(CurationWorkNotFoundError, match=r"^Work no-such-work not found$"):
        ask("no-such-work", chosen)
    with pytest.raises(
        CurationFileNotOnWorkError, match=rf"^File {foreign} does not belong to work {work}$"
    ):
        ask(work, foreign)
    with pytest.raises(CurationFileMissingError, match=rf"^File {missing} is missing from disk$"):
        ask(work, missing)
    with pytest.raises(
        FormatOverrideNotFoundError,
        match=rf"^Format override {created.id} not found for work {other_work}$",
    ):
        delete_format_override(other_work, created.id, overrides)

    delete_format_override(work, created.id, overrides)
    conn.commit()

    assert _overrides_of(conn, work) == {}


def test_delete_master(conn: Conn) -> None:
    work = _work(conn, "Lithium", _artist(conn, "Nirvana"))
    _master(conn, work, _file(conn, work, "lithium"), SelectionMethod.MANUAL)
    conn.commit()
    masters = RepositoryFactory(conn).song_masters

    delete_master(work, masters)
    conn.commit()

    assert _master_of(conn, work) is None
    with pytest.raises(SongMasterNotFoundError, match=rf"^No master found for work {work}$"):
        delete_master(work, masters)


# --- the transaction (S4's one transaction per write; E2, E3) ---------------------------------


@dataclass(frozen=True)
class _Seed:
    target: str
    source: str
    kept: UUID
    split_off: UUID
    moved: UUID
    override: UUID


def _seed_one_of_each(conn: Conn) -> _Seed:
    artist = _artist(conn, "Nirvana")
    target, source = _work(conn, "Lithium", artist), _work(conn, "lithium", artist)
    take = _recording(conn, target)
    kept = _file(conn, target, "kept", fmt="flac", recording_id=take)
    split_off = _file(conn, target, "split-off", recording_id=take)
    moved = _file(conn, source, "moved", recording_id=_recording(conn, source))
    override = _override(conn, target, "flac", kept, EARLY)
    _master(conn, target, kept, SelectionMethod.MANUAL)
    _line_match(conn, moved, source)
    conn.commit()
    return _Seed(target, source, kept, split_off, moved, override)


_TABLES = ("works", "recordings", "library_files", "song_masters", "format_overrides", "matches")


def _everything(conn: Conn) -> dict[str, list[tuple[object, ...]]]:
    """Every row of the tables the writes touch, in a stable order."""
    return {
        table: sorted(
            (tuple(str(v) for v in row.values()) for row in conn.execute(f"SELECT * FROM {table}")),
        )
        for table in _TABLES
    }


_WRITES: dict[str, Callable[[Conn, _Seed], object]] = {
    "merge": lambda conn, s: merge_works(s.target, [s.source], _repos(conn)),
    "split": lambda conn, s: split_file_off(s.target, s.split_off, _repos(conn)),
    "reassign": lambda conn, s: reassign_file(s.moved, s.target, _repos(conn)),
    "override-create": lambda conn, s: create_format_override(
        s.target,
        NewFormatOverride(format_name="mp3", preferred_file_id=s.split_off, notes=None),
        _repos(conn),
    ),
    "override-delete": lambda conn, s: delete_format_override(
        s.target, s.override, RepositoryFactory(conn).format_overrides
    ),
    "master-delete": lambda conn, s: delete_master(s.target, RepositoryFactory(conn).song_masters),
}


@pytest.mark.parametrize("write", sorted(_WRITES))
def test_writes_stay_in_the_callers_transaction(conn: Conn, migrated_db: str, write: str) -> None:
    """The service commits nothing: until the caller commits, another connection sees none of
    a write, and the caller's rollback undoes all of it."""
    seed = _seed_one_of_each(conn)
    before = _everything(conn)
    conn.commit()

    _WRITES[write](conn, seed)

    assert conn.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS
    assert _everything(conn) != before
    with psycopg.connect(migrated_db, row_factory=dict_row) as other:
        assert _everything(other) == before
    conn.rollback()
    assert _everything(conn) == before


def _wait_until_blocked(watch: Conn, pid: int, timeout_s: float = 20.0) -> None:
    """Return once backend *pid* waits on a lock; fail if it never does."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = watch.execute(
            "SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid,)
        ).fetchone()
        if row is not None and row["wait_event_type"] == "Lock":
            return
        time.sleep(0.01)
    pytest.fail(f"backend {pid} never waited on a lock")


def test_concurrent_merge(conn: Conn, migrated_db: str) -> None:
    """E2: two merges of one source, each on its own connection and transaction (SyncRepos).
    The second waits on the first's row lock, then finds nothing left to move."""
    artist = _artist(conn, "Nirvana")
    first_target = _work(conn, "Lithium", artist)
    second_target = _work(conn, "Lithium (again)", artist)
    source = _work(conn, "lithium", artist)
    moved = _file(conn, source, "moved")
    conn.commit()

    with (
        psycopg.connect(migrated_db, row_factory=dict_row) as first,
        psycopg.connect(migrated_db, row_factory=dict_row) as second,
        psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as watch,
    ):
        assert merge_works(first_target, [source], _repos(first)) == MergeResult(
            merged_file_count=1, deleted_work_count=1, dropped_override_count=0
        )
        outcome: list[MergeResult] = []
        waiter = threading.Thread(
            target=lambda: outcome.append(merge_works(second_target, [source], _repos(second))),
            daemon=True,
        )
        waiter.start()
        try:
            _wait_until_blocked(watch, second.info.backend_pid)
            first.commit()
            waiter.join(timeout=20)
        finally:
            if waiter.is_alive():  # never leave it waiting past the test
                watch.execute("SELECT pg_cancel_backend(%s)", (second.info.backend_pid,))
                waiter.join(timeout=5)
        assert not waiter.is_alive()
        second.commit()

    assert outcome == [NOTHING_MERGED]
    assert _work_of(conn, moved) == first_target
    assert _row(conn, "works", source) is None


@contextmanager
def _master_writes_refused(db_url: str) -> Iterator[None]:
    """Every song-master write fails in the database until the block ends (as phase 0a's)."""
    with psycopg.connect(db_url, autocommit=True) as admin:
        admin.execute(
            """CREATE OR REPLACE FUNCTION phase0c_refuse_master_write() RETURNS trigger
               LANGUAGE plpgsql AS $$
               BEGIN
                   RAISE EXCEPTION 'phase 0c: song master write refused';
               END $$"""
        )
        admin.execute(
            """CREATE TRIGGER phase0c_refuse_master_write
               BEFORE INSERT OR UPDATE ON song_masters
               FOR EACH ROW EXECUTE FUNCTION phase0c_refuse_master_write()"""
        )
        try:
            yield
        finally:
            admin.execute("DROP TRIGGER IF EXISTS phase0c_refuse_master_write ON song_masters")
            admin.execute("DROP FUNCTION IF EXISTS phase0c_refuse_master_write()")


def test_failure_rolls_back(conn: Conn, migrated_db: str) -> None:
    """E3: a merge that fails at its last write, the target's master, after moving the files,
    overrides and matches and deleting the source, leaves nothing behind once the caller rolls
    back: the service commits nothing of its own."""
    artist = _artist(conn, "Nirvana")
    target, source = _work(conn, "Lithium", artist), _work(conn, "lithium", artist)
    _file(conn, target, "kept", fmt="flac")
    moved = _file(conn, source, "moved")
    _override(conn, source, "mp3", moved, EARLY)
    line = _line_match(conn, moved, source)
    conn.commit()

    with _master_writes_refused(migrated_db):
        try:
            with pytest.raises(psycopg.errors.RaiseException, match="song master write refused"):
                merge_works(target, [source], _repos(conn))
        finally:
            conn.rollback()  # release song_masters, so the trigger can be dropped

    assert _work_of(conn, moved) == source
    assert _row(conn, "works", source) is not None
    assert _overrides_of(conn, source) == {"mp3": moved}
    assert _match_work(conn, line) == source
    assert _master_of(conn, target) is None
