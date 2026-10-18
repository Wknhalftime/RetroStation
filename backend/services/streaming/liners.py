"""Station liners (LR1-LR4): each station's own audio clips and the cadence they play at.

Liners are stored at ``<work_dir>/liners/<station_id>/<liner_id>.<format>``; the folder
helpers are pure paths. ``add_liner`` runs the storage order of Task 4:

1. the station must exist;
2. size check (``MAX_LINER_BYTES``), before anything is written;
3. stage the bytes under a partial name unique to this upload, ``<uuid4>.partial``, in the
   station's folder;
4. probe the partial (the format and length come from the content; no length limit);
5. install it as ``<liner_id>.<format>``;
6. hash the installed file (plan note 5): FLAC and MP3 as the library does, anything else as
   ``audio-sha256`` of the whole file;
7. the station already has this audio: discard the new file and answer ``DUPLICATE`` with the
   stored liner's id;
8. add the row, then commit.

This upload's partial is always discarded, and a failure after step 5 discards the installed
file. A disk failure is a ``LinerStorageError``. Every command commits its own writes through
``LinerPorts.commit``. A file a delete cannot remove (one a live engine holds open) is logged
as ``stream_liner_cleanup_failed`` and left behind; ``sweep_station_liners``, run after each
add and delete, removes it once it is older than ``PARTIAL_MAX_AGE``, along with a crash's
leftovers (Task 12a).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from uuid import UUID, uuid4

import structlog

from backend.domain.broadcast import BroadcastStation, UnknownStationError
from backend.domain.enums import AudioHashKind
from backend.domain.library import AudioHash
from backend.domain.liners import (
    LINER_FILE_NAME,
    MAX_LINER_BYTES,
    MAX_LINER_NAME,
    LinerCadence,
    LinerEntry,
    LinerNotFoundError,
    LinerStorageError,
    LinerTooLargeError,
    StationLiner,
)
from backend.domain.streaming import ClipFormat, ProbedClip
from backend.domain.system import StorageUnavailableError
from backend.repositories.broadcast_stations import BroadcastStationRepository
from backend.repositories.station_liners import StationLinerRepository
from backend.services.audio_hash import compute_audio_hash
from backend.services.streaming.sign_off import PARTIAL_MAX_AGE, ClipProbe, ClipUpload

logger = structlog.get_logger(__name__)

_LINERS_DIR = "liners"
_PARTIAL_SUFFIX = ".partial"
_FALLBACK_NAME = "liner"
_CLEANUP_FAILED = "stream_liner_cleanup_failed"
_PARTIAL_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.partial$"
)


def liner_folder(work_dir: Path) -> Path:
    """The root of every station's liners, under the stream work folder."""
    return work_dir / _LINERS_DIR


def station_liner_folder(work_dir: Path, station_id: UUID) -> Path:
    """The folder holding one station's liner files."""
    return _station_folder(liner_folder(work_dir), station_id)


def _station_folder(root: Path, station_id: UUID) -> Path:
    return root / str(station_id)


def liner_file(root: Path, liner: StationLiner) -> Path:
    """Where the liner's stored file lives under the liners root ``root``."""
    return _station_folder(root, liner.station_id) / liner.file_name


@dataclass(frozen=True)
class LinerPorts:
    """What the liner store is wired to at the composition root.

    ``folder`` is the liners root (``liner_folder``); ``commit`` commits the writes made so
    far; ``clock`` answers timezone-aware times; ``new_id`` names each new liner.
    """

    liners: StationLinerRepository
    stations: BroadcastStationRepository
    folder: Path
    probe: ClipProbe
    commit: Callable[[], None]
    clock: Callable[[], datetime]
    new_id: Callable[[], UUID] = uuid4


class UploadOutcome(StrEnum):
    """What an upload did: stored a new liner, or found its audio already stored."""

    ADDED = "added"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class LinerAdded:
    """The liner an upload answers with: the new one, or the one already holding its audio."""

    liner_id: UUID
    outcome: UploadOutcome


@dataclass(frozen=True)
class LinerLibrary:
    """A station's liners as the tab lists them, and the cadence they play at."""

    cadence: LinerCadence | None
    entries: list[LinerEntry]


@dataclass(frozen=True)
class LinerAudio:
    """A liner's stored file and its format, to serve as a preview."""

    path: Path
    format: ClipFormat


@dataclass(frozen=True)
class _Installed:
    """An upload installed under its liner's file name, with what the probe found."""

    liner_id: UUID
    path: Path
    probed: ProbedClip


def add_liner(ports: LinerPorts, station_id: UUID, upload: ClipUpload) -> LinerAdded:
    """Store ``upload`` as one of the station's liners, in the order above (LR2-LR4).

    Returns:
        The new liner's id (``ADDED``), or the id of the station's liner that already has
        this audio (``DUPLICATE``; nothing new is kept).

    The station's folder is swept afterwards (``sweep_station_liners``).

    Raises:
        UnknownStationError: no station has ``station_id``; nothing is written.
        LinerTooLargeError: the upload is larger than ``MAX_LINER_BYTES``; nothing is written.
        UnsupportedClipError, UnreadableClipError: from the probe; nothing is kept.
        DuplicateLinerError: a concurrent upload of the same audio was stored first (the lost
            race); nothing is kept.
        LinerStorageError: the file could not be written or read back; nothing is kept.
        StorageUnavailableError: from the commit; the new file is discarded.
    """
    _require_station(ports.stations, station_id)
    if len(upload.data) > MAX_LINER_BYTES:
        raise LinerTooLargeError(
            f"the liner is {len(upload.data)} bytes; a liner must be at most "
            f"{MAX_LINER_BYTES} bytes (200 MiB)"
        )
    folder = _station_folder(ports.folder, station_id)
    installed = _install_upload(folder, ports.new_id(), upload.data, ports.probe)
    added: LinerAdded | None = None
    try:
        added = _record(ports, station_id, upload.name, installed)
    finally:
        if added is None or added.outcome is UploadOutcome.DUPLICATE:
            _discard(installed.path)
    sweep_station_liners(ports, station_id)
    return added


def rename_liner(ports: LinerPorts, station_id: UUID, liner_id: UUID, name: str) -> None:
    """Change only the liner's display name, stripped, and commit (LR3).

    Raises:
        LinerNotFoundError: the station has no liner ``liner_id``.
        InvalidLinerValueError: the name is blank or longer than ``MAX_LINER_NAME``; nothing
            changes.
    """
    owned_liner(ports.liners, station_id, liner_id)
    ports.liners.rename(liner_id, name)
    ports.commit()


def delete_liner(ports: LinerPorts, station_id: UUID, liner_id: UUID) -> None:
    """Remove the liner's row and commit, then its file, best effort, then sweep the
    station's folder (LR3, Task 12a).

    Raises:
        LinerNotFoundError: the station has no liner ``liner_id``.
    """
    liner = owned_liner(ports.liners, station_id, liner_id)
    ports.liners.delete(liner_id)
    ports.commit()
    _discard(liner_file(ports.folder, liner))
    sweep_station_liners(ports, station_id)


def sweep_station_liners(ports: LinerPorts, station_id: UUID) -> None:
    """Remove the station folder's leftovers, best effort (LR3, Task 12a): files named like a
    liner whose row is gone (a delete's file the engine held open, or a crash between install
    and commit), and partials. Each goes only once older than ``PARTIAL_MAX_AGE``, so a
    concurrent upload's files are never touched; nothing else is. The station's stored file
    names are read once, and only when an old liner-named file is there; when that read fails,
    every liner file is kept. A failure is logged as ``stream_liner_cleanup_failed``, never
    raised.
    """
    # Ages are file mtimes against the real clock, as the sign-off sweep judges them; the
    # ports' clock only stamps rows.
    oldest_kept = time.time() - PARTIAL_MAX_AGE.total_seconds()
    entries = _folder_entries(_station_folder(ports.folder, station_id))
    old = [entry for entry in entries if _modified_before(entry, oldest_kept)]
    partials = [entry for entry in old if _PARTIAL_NAME.fullmatch(entry.name) is not None]
    liner_named = [entry for entry in old if LINER_FILE_NAME.fullmatch(entry.name) is not None]
    for entry in partials + _orphans(ports.liners, station_id, liner_named):
        _discard(entry)


def list_liners(ports: LinerPorts, station_id: UUID) -> LinerLibrary:
    """The station's cadence and its liners, each with the state of its cues (LR1, LR5).

    Raises:
        UnknownStationError: no station has ``station_id``.
    """
    station = _require_station(ports.stations, station_id)
    return LinerLibrary(station.liner_cadence, ports.liners.list_for_station(station_id))


def liner_audio(ports: LinerPorts, station_id: UUID, liner_id: UUID) -> LinerAudio:
    """The liner's stored file and format (LR5, preview).

    Raises:
        LinerNotFoundError: the station has no liner ``liner_id``, or its file is gone.
    """
    liner = owned_liner(ports.liners, station_id, liner_id)
    path = liner_file(ports.folder, liner)
    if not path.is_file():
        raise LinerNotFoundError(f"liner {liner_id} has no file at {path}")
    return LinerAudio(path=path, format=liner.format)


def set_liner_cadence(
    ports: LinerPorts, station_id: UUID, songs: int | None
) -> LinerCadence | None:
    """Store the station's cadence and commit; None turns its liners off (LR1).

    Returns:
        The stored cadence.

    Raises:
        UnknownStationError: no station has ``station_id``.
        InvalidLinerValueError: ``songs`` is outside 1-1,000; nothing changes.
    """
    _require_station(ports.stations, station_id)
    cadence = LinerCadence.from_setting(songs)
    ports.stations.set_liner_cadence(station_id, cadence)
    ports.commit()
    return cadence


def discard_station_liners(folder: Path, station_id: UUID) -> None:
    """Remove the station's liner folder under the liners root ``folder``, best effort: a
    failure is logged; a station with no folder is not an error (LR3)."""
    station_folder = _station_folder(folder, station_id)
    # The exists() check sits inside the try: a failed stat is logged like a failed delete,
    # since the station's row is already committed when this runs. The removal itself goes
    # entry by entry: each failure (a file an engine holds open) is logged and the rest go.
    try:
        if station_folder.exists():
            shutil.rmtree(station_folder, onexc=_log_cleanup_failure)
    except OSError as failed:
        logger.warning(_CLEANUP_FAILED, path=str(station_folder), error=str(failed))


def _log_cleanup_failure(
    _function: Callable[..., object], path: str, failed: BaseException
) -> None:
    logger.warning(_CLEANUP_FAILED, path=path, error=str(failed))


def liner_audio_hash(path: Path) -> AudioHash:
    """The liner file's audio hash (plan note 5): the library's for FLAC and MP3, otherwise
    ``audio-sha256`` of the whole file.

    Raises:
        OSError: the file cannot be read.
    """
    found = compute_audio_hash(path)
    return found if found is not None else _whole_file_sha256(path)


def _whole_file_sha256(path: Path) -> AudioHash:
    with path.open("rb") as fh:
        digest = hashlib.file_digest(fh, "sha256")
    return AudioHash(AudioHashKind.AUDIO_SHA256, digest.hexdigest())


def _require_station(stations: BroadcastStationRepository, station_id: UUID) -> BroadcastStation:
    station = stations.get_by_id(station_id)
    if station is None:
        raise UnknownStationError(f"no station has id {station_id}")
    return station


def owned_liner(liners: StationLinerRepository, station_id: UUID, liner_id: UUID) -> StationLiner:
    """The liner, reached only through its own station."""
    liner = liners.get(liner_id)
    if liner is None or liner.station_id != station_id:
        raise LinerNotFoundError(f"station {station_id} has no liner {liner_id}")
    return liner


def _install_upload(folder: Path, liner_id: UUID, data: bytes, probe: ClipProbe) -> _Installed:
    """Stage, probe and install the bytes as ``<liner_id>.<format>``; the partial is always
    discarded."""
    partial = folder / f"{uuid4()}{_PARTIAL_SUFFIX}"
    try:
        _stage(partial, data)
        probed = probe(partial)
        target = folder / f"{liner_id}.{probed.format}"
        _move(partial, target)
    finally:
        _discard(partial)
    return _Installed(liner_id=liner_id, path=target, probed=probed)


def _record(
    ports: LinerPorts, station_id: UUID, upload_name: str, installed: _Installed
) -> LinerAdded:
    """Add the installed file's row and commit, unless the station has its audio already."""
    audio_hash = _hash_installed(installed.path)
    existing = ports.liners.find_by_hash(station_id, audio_hash)
    if existing is not None:
        return LinerAdded(liner_id=existing.id, outcome=UploadOutcome.DUPLICATE)
    ports.liners.add(
        StationLiner(
            id=installed.liner_id,
            station_id=station_id,
            name=_display_name(upload_name),
            file_name=installed.path.name,
            format=installed.probed.format,
            audio_hash=audio_hash,
            duration_ms=installed.probed.span_ms,
            markers=None,
            uploaded_at=ports.clock(),
        )
    )
    ports.commit()
    return LinerAdded(liner_id=installed.liner_id, outcome=UploadOutcome.ADDED)


def _display_name(upload_name: str) -> str:
    """The upload's name without its folders (either separator) or extension, cut to
    ``MAX_LINER_NAME``; ``"liner"`` when nothing is left."""
    stem = PureWindowsPath(upload_name).stem.strip()
    return (stem or _FALLBACK_NAME)[:MAX_LINER_NAME]


def _stage(partial: Path, data: bytes) -> None:
    try:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(data)
    except OSError as failed:
        raise LinerStorageError(f"the liner could not be stored: {failed}") from failed


def _move(partial: Path, target: Path) -> None:
    try:
        os.replace(partial, target)
    except OSError as failed:
        raise LinerStorageError(f"the liner could not be stored: {failed}") from failed


def _hash_installed(path: Path) -> AudioHash:
    try:
        return liner_audio_hash(path)
    except OSError as failed:
        raise LinerStorageError(f"the liner could not be read back: {failed}") from failed


def _folder_entries(folder: Path) -> list[Path]:
    """The folder's entries; none when it is missing or cannot be listed (logged)."""
    if not folder.is_dir():
        return []
    try:
        return list(folder.iterdir())
    except OSError as failed:
        logger.warning(_CLEANUP_FAILED, path=str(folder), error=str(failed))
        return []


def _orphans(
    liners: StationLinerRepository, station_id: UUID, liner_named: list[Path]
) -> list[Path]:
    """The files in ``liner_named`` that no row of the station's names, read in one query (none
    when there is nothing to check). A lost connection is logged and keeps every file: an
    unknown is never deleted."""
    if not liner_named:
        return []
    try:
        stored = {entry.liner.file_name for entry in liners.list_for_station(station_id)}
    except StorageUnavailableError as lost:
        logger.warning(_CLEANUP_FAILED, station_id=str(station_id), error=str(lost))
        return []
    return [entry for entry in liner_named if entry.name not in stored]


def _modified_before(entry: Path, oldest_kept: float) -> bool:
    try:
        return entry.stat().st_mtime < oldest_kept
    except OSError:
        return False  # gone already, or unreadable: the next sweep looks again


def _discard(path: Path) -> None:
    """Delete ``path`` if it exists; a failure (a file a live engine holds open) is logged and
    the file left behind."""
    try:
        path.unlink(missing_ok=True)
    except OSError as failed:
        logger.warning(_CLEANUP_FAILED, path=str(path), error=str(failed))
