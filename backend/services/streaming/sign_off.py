"""The user's sign-off clip store (D26; PG3; review rulings I1-I3, M4).

A clip is stored content-named, ``<first 16 hex of its SHA-256>.<detected format>``, in the
sign-off folder under the streaming work directory, and recorded in the ``stream_sign_off``
user setting. The upload's own name is kept only as the name the page shows: it never decides
the format or the path (I2).

``save_sign_off`` runs the storage order of the plan's design note 5:

1. size check;
2. stage the bytes under a partial name unique to this upload, ``<uuid4>.partial``;
3. probe the partial (the format and length come from the content);
4. length check;
5. install: if the content-named target already exists, the partial is discarded; otherwise
   the partial is moved onto it with ``os.replace``;
6. upsert the setting;
7. commit;
8. only then delete the previous clip's file, if its name differs from the new one (I1);
9. sweep the folder, best effort: content-named clips that are not current and partials older
   than ``PARTIAL_MAX_AGE``.

A disk failure in steps 2 or 5 is a ``ClipStorageError``; in steps 8 or 9 it is logged
(``stream_sign_off_cleanup_failed``) and left for the next sweep (I3). This upload's partial
is always discarded.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import structlog

from backend.domain.streaming import (
    MAX_CLIP_BYTES,
    MAX_CLIP_MS,
    MIN_CLIP_MS,
    SIGN_OFF_FILE_NAME,
    ClipLengthError,
    ClipStorageError,
    ClipTooLargeError,
    InvalidStreamValueError,
    ProbedClip,
    SignOff,
)
from backend.domain.system import UserSetting
from backend.repositories.user_settings import UserSettingRepository

logger = structlog.get_logger(__name__)

SIGN_OFF_KEY = "stream_sign_off"
"""The user setting the clip is stored under; written only by the sign-off's own path (PG2)."""

PARTIAL_MAX_AGE = timedelta(hours=1)
"""A partial older than this belongs to no upload still running, so the sweep removes it."""

_PARTIAL_SUFFIX = ".partial"
_PARTIAL_NAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.partial$"
)
_CONTENT_HASH_CHARS = 16

type ClipProbe = Callable[[Path], ProbedClip]
"""Reads a staged file's audio content and reports its format and length (I2)."""


@dataclass(frozen=True)
class ClipUpload:
    """An uploaded clip: the name it was sent under (shown on the page only) and its bytes."""

    name: str
    data: bytes


def sign_off_folder(work_dir: Path) -> Path:
    """Where sign-off clips are stored, under the streaming work directory."""
    return work_dir / "sign-off"


@dataclass(frozen=True)
class SignOffPorts:
    """What the sign-off's read and write paths are wired to at the composition root.

    ``commit`` commits the settings write; a database failure is a ``ClipStorageError``.
    """

    settings: UserSettingRepository
    folder: Path
    probe: ClipProbe
    commit: Callable[[], None]


def stored_sign_off(settings: UserSettingRepository) -> SignOff | None:
    """The stored sign-off clip, or ``None`` when there is none.

    Raises:
        InvalidStreamValueError: the stored setting is not a sign-off.
    """
    found = settings.get(SIGN_OFF_KEY)
    return None if found is None else SignOff.from_setting(found.value)


def save_sign_off(ports: SignOffPorts, upload: ClipUpload) -> SignOff:
    """Store ``upload`` as the sign-off clip, replacing any previous one (design note 5).

    Returns:
        The stored clip.

    Raises:
        ClipTooLargeError: the clip is larger than ``MAX_CLIP_BYTES``; nothing is written.
        UnsupportedClipError, UnreadableClipError: from the probe; nothing is kept.
        ClipLengthError: the clip lasts less than 1 second or more than 5 minutes.
        ClipStorageError: the clip or its setting could not be stored; the stored clip and
            its file are unchanged.
        StorageUnavailableError: the settings connection was lost before the commit; the
            stored clip is unchanged and the new file is left for the next sweep.
    """
    if len(upload.data) > MAX_CLIP_BYTES:
        raise ClipTooLargeError(
            f"the clip is {len(upload.data)} bytes; a sign-off must be at most "
            f"{MAX_CLIP_BYTES} bytes (25 MiB)"
        )
    partial = ports.folder / f"{uuid4()}{_PARTIAL_SUFFIX}"
    try:
        _stage(partial, upload.data)
        clip = _content_named(upload, ports.probe(partial))
        _install(partial, ports.folder / clip.file_name)
    finally:
        _discard(partial)
    previous = _previous_file_name(ports.settings)
    ports.settings.upsert(UserSetting(key=SIGN_OFF_KEY, value=clip.to_setting()))
    ports.commit()
    if previous is not None and previous != clip.file_name:
        _discard(ports.folder / previous)
    _sweep(ports.folder, keep=_names(clip.file_name) | _names(previous))
    return clip


def remove_sign_off(ports: SignOffPorts) -> None:
    """Clear the sign-off clip: delete the setting, commit, then delete its file and sweep.

    Removing when no clip is stored is not an error.

    Raises:
        ClipStorageError: the commit failed; the stored clip and its file are unchanged.
        StorageUnavailableError: the settings connection was lost before the commit; the
            stored clip and its file are unchanged.
    """
    previous = _previous_file_name(ports.settings)
    ports.settings.delete(SIGN_OFF_KEY)
    ports.commit()
    if previous is not None:
        _discard(ports.folder / previous)
    _sweep(ports.folder, keep=_names(previous))


def _content_named(upload: ClipUpload, probed: ProbedClip) -> SignOff:
    if not MIN_CLIP_MS <= probed.span_ms <= MAX_CLIP_MS:
        raise ClipLengthError(
            f"the clip lasts {probed.span_ms / 1000:g} s; a sign-off must last "
            f"1 second to 5 minutes"
        )
    digest = hashlib.sha256(upload.data).hexdigest()[:_CONTENT_HASH_CHARS]
    return SignOff(
        file_name=f"{digest}.{probed.format}",
        format=probed.format,
        span_ms=probed.span_ms,
        name=upload.name,
    )


def _stage(partial: Path, data: bytes) -> None:
    try:
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_bytes(data)
    except OSError as failed:
        raise ClipStorageError(f"the clip could not be stored: {failed}") from failed


def _install(partial: Path, target: Path) -> None:
    """Move the partial onto its content name; the same content already stored is kept."""
    if target.exists():
        return
    try:
        os.replace(partial, target)
    except OSError as failed:
        raise ClipStorageError(f"the clip could not be stored: {failed}") from failed


def _previous_file_name(settings: UserSettingRepository) -> str | None:
    """The stored clip's file name; a setting that is not a sign-off names no file (the sweep
    removes whatever it left)."""
    try:
        previous = stored_sign_off(settings)
    except InvalidStreamValueError:
        return None
    return None if previous is None else previous.file_name


def _names(file_name: str | None) -> frozenset[str]:
    return frozenset() if file_name is None else frozenset({file_name})


def _discard(path: Path) -> None:
    """Delete ``path`` if it exists; a failure (a file a live engine holds open) is logged and
    left for the next sweep (I3)."""
    try:
        path.unlink(missing_ok=True)
    except OSError as failed:
        logger.warning("stream_sign_off_cleanup_failed", path=str(path), error=str(failed))


def _sweep(folder: Path, keep: frozenset[str]) -> None:
    """Remove the folder's leftovers, best effort: content-named clips not in ``keep`` and
    partials older than ``PARTIAL_MAX_AGE``. Other files are never touched."""
    if not folder.is_dir():
        return
    try:
        entries = [entry for entry in folder.iterdir() if entry.name not in keep]
    except OSError as failed:
        logger.warning("stream_sign_off_cleanup_failed", path=str(folder), error=str(failed))
        return
    oldest_kept = time.time() - PARTIAL_MAX_AGE.total_seconds()
    for entry in entries:
        if SIGN_OFF_FILE_NAME.fullmatch(entry.name) or _is_stale_partial(entry, oldest_kept):
            _discard(entry)


def _is_stale_partial(entry: Path, oldest_kept: float) -> bool:
    if _PARTIAL_NAME.fullmatch(entry.name) is None:
        return False
    try:
        return entry.stat().st_mtime < oldest_kept
    except OSError:
        return False  # gone already, or unreadable: the next sweep looks again
