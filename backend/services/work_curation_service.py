"""The Library's works writes: merge, split, reassign, format overrides, the master delete, and
the work page's release of pinned lines (3b PR 5a).

Moved from ``routers/library/works.py`` in phase 0c (C75), with the router's behaviour,
oddities included: the master recalculation replaces a manual pick that is no longer a present
file of the work. Phase 3b PR 1 changed the merge's master (3b SPEC S3, E1, D8-D11): a
hand-picked master, from the target or a source, beats an automatic one. Each write runs in
the caller's transaction (a sync route's ``SyncRepos``) and commits nothing; each refusal is a
:class:`WorkCurationError` whose message is the route's detail.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from uuid import UUID, uuid4

import structlog

from backend.domain.catalog import Recording, Work
from backend.domain.credit_picks import WorkCredits
from backend.domain.curation import (
    CurationFileMissingError,
    CurationFileNotFoundError,
    CurationFileNotOnWorkError,
    CurationWorkNotFoundError,
    FileAlreadyOnWorkError,
    FormatOverride,
    FormatOverrideExistsError,
    FormatOverrideNotFoundError,
    FormatOverrideNotStoredError,
    MasterCandidate,
    MergeResult,
    MergeTargetInSourcesError,
    NewFormatOverride,
    PinNotOnWorkError,
    ReassignResult,
    SongMaster,
    SongMasterNotFoundError,
    SplitResult,
)
from backend.domain.enums import CatalogSource, FileStatus, SelectionMethod, VersionType
from backend.domain.performances import choose_performance
from backend.repositories.artist_catalog import ArtistCatalogRepository
from backend.repositories.artist_merges import ArtistMergeRepository
from backend.repositories.credit_picks import CreditPickRepository
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.log_name_links import LogNameLinkRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.performances import PerformanceRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.work_curation import WorkCurationRepository
from backend.repositories.work_merges import MasterLine, WorkMergeRepository
from backend.repositories.works import WorkRepository
from backend.services.log_name_link_service import LogNameLinkService
from backend.services.master_selection_service import score_file_row
from backend.services.performance_service import move_out, place
from backend.services.pin_upkeep import release_pins
from backend.services.song_match_service import SongMatchService

logger = structlog.get_logger()

_NOTHING_MERGED = MergeResult(merged_file_count=0, deleted_work_count=0, dropped_override_count=0)


@dataclass(frozen=True)
class WorkCurationRepos:
    """The repositories the works writes run through, on one connection."""

    curation: WorkCurationRepository
    works: WorkRepository
    files: LibraryFileRepository
    recordings: RecordingRepository
    song_masters: SongMasterRepository
    overrides: FormatOverrideRepository
    matches: MatchRepository
    artists: ArtistCatalogRepository
    links: LogNameLinkRepository
    merges: ArtistMergeRepository
    # Phase 3a (S3): the files' performances; None skips them.
    performances: PerformanceRepository | None = None
    # Phase 3b PR 3 (S3, S4): the lines a merge holds, and the credits that pick their act;
    # None skips pins.
    lines: WorkMergeRepository | None = None
    credits: CreditPickRepository | None = None


def _work(work_id: str, works: WorkRepository) -> Work:
    work = works.get_by_id(work_id)
    if work is None:
        raise CurationWorkNotFoundError(f"Work {work_id} not found")
    return work


def _recording_of_version(
    work_id: str, version_type: VersionType, recordings: RecordingRepository
) -> str | None:
    """The work's recording of *version_type* (at most one: UNIQUE (work_id, version_type))."""
    return next(
        (r.id for r in recordings.get_by_work(work_id) if r.version_type == version_type), None
    )


# --- the master recalculation ------------------------------------------------------------------


def _score(candidate: MasterCandidate) -> tuple[int, int, int]:
    return score_file_row(asdict(candidate))


def _recalculate_master(work_id: str, repos: WorkCurationRepos) -> None:
    """Re-pick the work's automatic master from its present files.

    A manual master stays only while its file is a present file of the work; otherwise the
    best-scoring present file replaces it as the automatic pick, whatever the stored
    master's method. With no present file, a master on one of the work's own files stays
    (the file may come back) and one on a file of another work is removed.
    """
    master = repos.song_masters.get_by_work(work_id)
    if master is not None and master.selection_method == SelectionMethod.MANUAL:
        chosen = repos.files.get_by_id(master.preferred_file_id)
        if (
            chosen is not None
            and chosen.work_id == work_id
            and chosen.file_status == FileStatus.PRESENT
        ):
            return
    candidates = repos.curation.present_master_candidates(work_id)
    if not candidates:
        repos.curation.delete_master_off_work(work_id)
        return
    best = max(candidates, key=_score)
    master_id = master.id if master is not None else uuid4()
    repos.curation.store_auto_master(master_id, work_id, best.id, _score(best)[0])


def _settle_left_work(work_id: str, repos: WorkCurationRepos) -> bool:
    """Settle the work a file just left; whether it was deleted.

    With files left, its master is re-picked. With none, a match still on it follows its file
    to that file's work and its file-less recordings go, as before; then the one deletion rule
    decides (3b D7, D16; C53): nothing else pointing at it, it goes with its master; else it
    stays, listed for Lance, losing only a master left on another work's file."""
    if repos.files.get_by_work(work_id):
        _recalculate_master(work_id, repos)
        return False
    SongMatchService(repos.matches).release_work(work_id)
    repos.curation.delete_fileless_recordings_of_work(work_id)
    if repos.works.delete_if_empty(work_id):
        return True
    _recalculate_master(work_id, repos)
    return False


# --- merge -------------------------------------------------------------------------------------


def _existing_works(work_ids: list[str], works: WorkRepository) -> list[Work]:
    found = (works.get_by_id(work_id) for work_id in dict.fromkeys(work_ids))
    return [work for work in found if work is not None]


def _consolidate_recordings(
    source_ids: list[str], target_id: str, repos: WorkCurationRepos
) -> None:
    """Move the sources' recordings to the target; one of a version the target already has
    hands its files to the target's and is deleted."""
    for recording in repos.curation.recordings_of_works(source_ids):
        kept = _recording_of_version(target_id, recording.version_type, repos.recordings)
        if kept is not None:
            repos.curation.move_recording_files(recording.id, kept)
            repos.curation.delete_recording(recording.id)
        else:
            repos.curation.move_recording(recording.id, target_id)


def _delete_unused_local_artists(artist_ids: set[str], repos: WorkCurationRepos) -> None:
    """Delete each local artist left with no work, no log-name link in any state (AUD-R035,
    which changes AUD-R004; phase 1a S11), no merge on either side (phase 2a S11) and no
    performance crediting it (3b D19: the source's act still performs a copy of the merged
    work)."""
    links = LogNameLinkService(repos.links, repos.artists)
    for artist_id in artist_ids:
        artist = repos.artists.get_by_id(artist_id)
        if artist is None or artist.origin != CatalogSource.LOCAL:
            continue
        if repos.works.get_by_artist(artist_id) or repos.merges.is_merged(artist_id):
            continue
        if repos.performances is not None and repos.performances.credits_artist(artist_id):
            continue
        if not links.is_linked(artist_id):
            repos.curation.delete_artist(artist_id)


def _is_valid_pick(master: SongMaster, work_ids: list[str], files: LibraryFileRepository) -> bool:
    """Whether *master* is hand-picked and its file a present file of one of *work_ids*
    (3b SPEC D10: a pick that couldn't play doesn't compete)."""
    if master.selection_method != SelectionMethod.MANUAL:
        return False
    chosen = files.get_by_id(master.preferred_file_id)
    return (
        chosen is not None
        and chosen.work_id in work_ids
        and chosen.file_status == FileStatus.PRESENT
    )


def _hand_picked_master(
    target_id: str, source_ids: list[str], repos: WorkCurationRepos
) -> SongMaster | None:
    """The hand-picked master the merged work keeps (3b SPEC S3, E1), or None: then the
    recalculation re-picks the master from the merged work's present files, stored automatic
    (D8). Read before anything moves.

    Between two or more valid picks, the work with more matched plays wins (D9), then the
    target, then the source listed first.
    """
    order = [target_id, *source_ids]
    picks: dict[str, SongMaster] = {}
    for work_id in order:
        master = repos.song_masters.get_by_work(work_id)
        if master is not None and _is_valid_pick(master, order, repos.files):
            picks[work_id] = master
    if len(picks) < 2:
        return next(iter(picks.values()), None)
    plays = repos.curation.matched_plays(list(picks))
    # More plays first; on a tie the one earlier in `order`: the target, then the listed sources.
    return picks[max(picks, key=lambda work_id: (plays.get(work_id, 0), -order.index(work_id)))]


def _carry_master(master: SongMaster, target_id: str, repos: WorkCurationRepos) -> None:
    """Store a source's hand-picked master as the target's, still hand-picked (D11)."""
    current = repos.song_masters.get_by_work(target_id)
    repos.song_masters.replace(
        SongMaster(
            id=current.id if current is not None else uuid4(),
            work_id=target_id,
            preferred_file_id=master.preferred_file_id,
            selection_method=SelectionMethod.MANUAL,
            score=master.score,
        )
    )


def _own_act(
    line: MasterLine,
    work: WorkCredits | None,
    credits: CreditPickRepository,
    performances: PerformanceRepository,
) -> UUID | None:
    """S4: the non-default performance of the merged work the line's links pick, when it holds
    the file the line played; else None (the line is pinned)."""
    if work is None or not line.artists:
        return None
    artists = set(line.artists) | {a for p in work.performances for a in p.artists}
    choice = choose_performance(
        line.artists,
        work.performances,
        work.default_performance_id,
        primary_of=credits.primaries(artists),
    )
    if choice.performance_id in (None, work.default_performance_id):
        return None
    played = performances.file_for_performance(line.master_file_id)
    if played is None or played.performance_id != choice.performance_id:
        return None
    return choice.performance_id


def _hold_lines(target_id: str, held: list[MasterLine], repos: WorkCurationRepos) -> None:
    """S3, S4: each line whose master's file changed keeps it: its own act's performance stored
    with that file preferred, or a pin on the file."""
    master = repos.song_masters.get_by_work(target_id)
    now = master.preferred_file_id if master is not None else None
    changed = [line for line in held if line.master_file_id != now]
    if not changed:
        return
    credits, performances = repos.credits, repos.performances
    work = credits.work_credits([target_id]).get(target_id) if credits is not None else None
    song_matches = SongMatchService(repos.matches)
    pins: dict[UUID, list[UUID]] = {}
    for line in changed:
        own = (
            _own_act(line, work, credits, performances)
            if credits is not None and performances is not None
            else None
        )
        if own is not None and performances is not None:
            performances.set_preferred_copy(own, line.master_file_id)
            song_matches.store_performance(line.match_id, own)
        else:
            pins.setdefault(line.master_file_id, []).append(line.match_id)
    for file_id, match_ids in pins.items():
        song_matches.pin(sorted(set(match_ids)), file_id)


def merge_works(
    target_id: str, source_work_ids: list[str], repos: WorkCurationRepos
) -> MergeResult:
    """Fold the source works into the target: files, overrides of both kinds, version-entry
    states (combined, D6), recordings, matches; the sources and their masters go. The merged
    work keeps the hand-picked master 3b's S3 picks, else its master is re-picked (D8)."""
    target = _work(target_id, repos.works)
    if target_id in source_work_ids:
        raise MergeTargetInSourcesError("Target work ID must not appear in source_work_ids")
    sources = _existing_works(source_work_ids, repos.works) if source_work_ids else []
    if not sources:
        return _NOTHING_MERGED

    source_ids = [work.id for work in sources]
    kept = _hand_picked_master(target_id, source_ids, repos)
    held = repos.lines.master_lines([target_id, *source_ids]) if repos.lines is not None else []
    moving = [f.id for work_id in source_ids for f in repos.files.get_by_work(work_id)]
    left = move_out(moving, repos.performances)
    merged = repos.curation.move_files_of_works(source_ids, target_id)
    dropped = repos.curation.drop_colliding_overrides(source_ids, target_id)
    repos.curation.move_overrides_of_works(source_ids, target_id)
    repos.curation.move_version_states_of_works(source_ids, target_id)
    _consolidate_recordings(source_ids, target_id, repos)
    song_matches = SongMatchService(repos.matches)
    song_matches.follow_works(source_ids, target_id)
    place(moving, left, repos.performances, song_matches)
    repos.curation.delete_masters_of_works(source_ids)
    deleted = repos.curation.delete_works(source_ids)
    if kept is not None and kept.work_id != target_id:
        _carry_master(kept, target_id, repos)
    _recalculate_master(target_id, repos)
    _hold_lines(target_id, held, repos)
    _delete_unused_local_artists({work.artist_id for work in sources} - {target.artist_id}, repos)
    return MergeResult(
        merged_file_count=merged, deleted_work_count=deleted, dropped_override_count=dropped
    )


# --- split ---------------------------------------------------------------------------------------


def split_file_off(work_id: str, file_id: UUID, repos: WorkCurationRepos) -> SplitResult:
    """Move a file onto a new work of its own (same title and artist), with a copy of its
    recording and each version text on its lines as seen (D6); the work it left is deleted
    if emptied, else its master is re-picked. Overrides stay behind (D1)."""
    work = _work(work_id, repos.works)
    file = repos.files.get_by_id(file_id)
    if file is None or file.work_id != work_id:
        raise CurationFileNotOnWorkError(f"File {file_id} does not belong to work {work_id}")

    new_work_id = repos.works.create_local(work.title, work.artist_id)
    if file.recording_id is not None:
        old = repos.recordings.get_by_id(file.recording_id)
        copy = Recording(
            id=str(uuid4()),
            title=old.title if old is not None else work.title,
            work_id=new_work_id,
            version_type=old.version_type if old is not None else VersionType.ORIGINAL,
            duration_ms=old.duration_ms if old is not None else None,
            needs_enhancement=False,
        )
        repos.curation.insert_recording(copy)
        repos.curation.set_file_recording(file_id, copy.id)
    left = move_out([file_id], repos.performances)
    release_pins(file_id, repos.matches, why="file split off")
    repos.files.update_work_id(file_id, new_work_id)
    song_matches = SongMatchService(repos.matches)
    song_matches.follow_file(file_id, new_work_id)
    place([file_id], left, repos.performances, song_matches)
    repos.matches.see_version_states(file_id, work_id, new_work_id)
    repos.curation.store_split_master(uuid4(), new_work_id, file_id)
    left_deleted = _settle_left_work(work_id, repos)
    return SplitResult(new_work_id=new_work_id, old_work_deleted=left_deleted)


# --- reassign ------------------------------------------------------------------------------------


def _repoint_recording(
    file_id: UUID, recording_id: str, target_id: str, repos: WorkCurationRepos
) -> None:
    """Put the file on a recording of the target work.

    The target's recording of the same version comes first; else a recording shared with
    other files is copied onto the target (title, version, duration) and keeps them, and one
    the file holds alone moves itself.
    """
    shared = repos.curation.count_other_files_on_recording(recording_id, file_id) > 0
    old = repos.recordings.get_by_id(recording_id)
    version = old.version_type if old is not None else VersionType.ORIGINAL
    kept = _recording_of_version(target_id, version, repos.recordings)
    if kept is not None:
        repos.curation.set_file_recording(file_id, kept)
    elif shared:
        copy = Recording(
            id=str(uuid4()),
            title=old.title if old is not None else "",
            work_id=target_id,
            version_type=version,
            duration_ms=old.duration_ms if old is not None else None,
            needs_enhancement=False,
        )
        repos.curation.insert_recording(copy)
        repos.curation.set_file_recording(file_id, copy.id)
    else:
        repos.curation.move_recording(recording_id, target_id)


def reassign_file(file_id: UUID, work_id: str, repos: WorkCurationRepos) -> ReassignResult:
    """Move a file onto another work; its recording and matches follow, the target gets each
    version text on its lines as seen (D6) and re-picks its master, and the work it left is
    deleted if emptied, else its master is re-picked. Overrides stay behind (D1)."""
    file = repos.files.get_by_id(file_id)
    if file is None:
        raise CurationFileNotFoundError(f"File {file_id} not found")
    _work(work_id, repos.works)
    if file.work_id == work_id:
        raise FileAlreadyOnWorkError(f"File {file_id} is already assigned to work {work_id}")

    if file.recording_id is not None:
        _repoint_recording(file_id, file.recording_id, work_id, repos)
    left = move_out([file_id], repos.performances)
    release_pins(file_id, repos.matches, why="file reassigned")
    repos.files.update_work_id(file_id, work_id)
    song_matches = SongMatchService(repos.matches)
    song_matches.follow_file(file_id, work_id)
    place([file_id], left, repos.performances, song_matches)
    _recalculate_master(work_id, repos)
    left_deleted = False
    if file.work_id is not None:
        repos.matches.see_version_states(file_id, file.work_id, work_id)
        left_deleted = _settle_left_work(file.work_id, repos)
    return ReassignResult(old_work_id=file.work_id, old_work_deleted=left_deleted)


# --- format overrides and the master delete --------------------------------------------------


def create_format_override(
    work_id: str, new: NewFormatOverride, repos: WorkCurationRepos
) -> FormatOverride:
    """Store a per-format preferred file for a work: a present file of the work, one per
    format."""
    _work(work_id, repos.works)
    file = repos.files.get_by_id(new.preferred_file_id)
    if file is None or file.work_id != work_id:
        raise CurationFileNotOnWorkError(
            f"File {new.preferred_file_id} does not belong to work {work_id}"
        )
    if file.file_status != FileStatus.PRESENT:
        raise CurationFileMissingError(f"File {new.preferred_file_id} is missing from disk")
    override = FormatOverride(
        id=uuid4(),
        work_id=work_id,
        format_name=new.format_name,
        preferred_file_id=new.preferred_file_id,
        notes=new.notes,
    )
    if not repos.curation.insert_override_if_absent(override):
        raise FormatOverrideExistsError(
            f"Format override for work {work_id} and format '{new.format_name}' already exists"
        )
    stored = repos.overrides.get(work_id, new.format_name)
    if stored is None:
        raise FormatOverrideNotStoredError(f"Format override {override.id} was not stored")
    return stored


def delete_format_override(
    work_id: str, override_id: UUID, overrides: FormatOverrideRepository
) -> None:
    """Delete one of the work's format overrides."""
    if all(o.id != override_id for o in overrides.list_by_work(work_id)):
        raise FormatOverrideNotFoundError(
            f"Format override {override_id} not found for work {work_id}"
        )
    overrides.delete(override_id)


def delete_master(work_id: str, song_masters: SongMasterRepository) -> None:
    """Delete the work's song master, whatever its method."""
    if song_masters.get_by_work(work_id) is None:
        raise SongMasterNotFoundError(f"No master found for work {work_id}")
    song_masters.delete_by_work(work_id)


# --- release on the work page --------------------------------------------------------------------


def release_line_pins(work_id: str, identity_ids: Sequence[UUID], repos: WorkCurationRepos) -> int:
    """Release on the work page (3b PR 5a; S8, D41 d, e): each named line's pins on copies of
    the work go, logged, and the lines take the normal path; how many lines. Nothing is
    released when one of them pins no copy of the work."""
    _work(work_id, repos.works)
    lines = sorted(set(identity_ids))
    held: dict[UUID, list[UUID]] = {line: [] for line in lines}
    for pin in repos.matches.pins_of_lines(lines):
        pinned = repos.files.get_by_id(pin.file_id)
        if pinned is not None and pinned.work_id == work_id:
            held[pin.identity_id].append(pin.match_id)
    unpinned = [str(line) for line, match_ids in held.items() if not match_ids]
    if unpinned:
        raise PinNotOnWorkError(f"Line {', '.join(unpinned)} pins no copy of work {work_id}")
    SongMatchService(repos.matches).unpin([m for match_ids in held.values() for m in match_ids])
    logger.info(
        "pins_released",
        work_id=work_id,
        identity_ids=[str(line) for line in lines],
        lines=len(lines),
        reason="released on the work page",
    )
    return len(lines)
