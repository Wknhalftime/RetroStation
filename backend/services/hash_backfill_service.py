"""Audio-hash backfill — fingerprint the rows a scan could not.

A scan reads only tags, and a FLAC's stored MD5 comes with them. MP3s, FLACs
without a stored MD5, and rows indexed before audio hashes existed wait here.
This pass fingerprints each one, one file at a time in path order:
interleaving reads of several files cost the library's SATA SSD ~70% of its
throughput (PR #70).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog

from backend.domain.library import LibraryFile
from backend.repositories.library_files import LibraryFileRepository
from backend.services.audio_hash import AudioHasher, compute_audio_hash
from backend.services.library_scan_service import disk_stat  # ⚠ AUD-009

logger = structlog.get_logger()


class BackfillOutcome(StrEnum):
    HASHED = "hashed"
    # The file's stat moved since it was indexed, or the row was rewritten
    # while we read it. Left to the watcher, which re-reads changed files.
    CHANGED = "changed"
    # Gone, unreadable, or holding no audio. Gone files are the watcher's to
    # mark missing; the cursor moves past the rest.
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class HashBackfillBatch:
    """Outcome counts for one batch, and the cursor for the next."""

    hashed: int = 0
    changed: int = 0
    unreadable: int = 0
    # Path of the last row visited; None when no rows were left after the cursor.
    last_path: str | None = None

    @property
    def exhausted(self) -> bool:
        return self.last_path is None


def _backfill_one(
    row: LibraryFile,
    file_repo: LibraryFileRepository,
    hash_audio: AudioHasher,
) -> BackfillOutcome:
    """Fingerprint one row's file if it is still exactly what was indexed."""
    path = Path(row.file_path)
    try:
        before = disk_stat(path)
        if (before.size, before.mtime_ns) != (row.file_size, row.file_mtime_ns):
            return BackfillOutcome.CHANGED
        audio_hash = hash_audio(path)
        # A write during the read must not pin a hash of new bytes to old tags.
        if disk_stat(path) != before:
            return BackfillOutcome.CHANGED
    except OSError as exc:
        # DEBUG, not WARNING: a row no run can hash is revisited by every
        # periodic resume; the run's `unreadable` total already reports it.
        logger.debug("hash_backfill_unreadable", path=row.file_path, error=str(exc))
        return BackfillOutcome.UNREADABLE
    if audio_hash is None:
        logger.debug("hash_backfill_no_audio", path=row.file_path)
        return BackfillOutcome.UNREADABLE
    if not file_repo.set_audio_hash(row.id, audio_hash, before.size, before.mtime_ns):
        return BackfillOutcome.CHANGED
    return BackfillOutcome.HASHED


def backfill_hash_batch(
    file_repo: LibraryFileRepository,
    *,
    after_path: str | None,
    limit: int,
    hash_audio: AudioHasher = compute_audio_hash,
) -> HashBackfillBatch:
    """Fingerprint up to *limit* PRESENT FLAC/MP3 rows after *after_path*, in path order.

    A skipped row keeps no hash and this cursor moves past it, so a run
    always ends; the watcher or the next run picks it up.
    """
    rows = file_repo.get_audio_unhashed_after(after_path, limit)
    counts = dict.fromkeys(BackfillOutcome, 0)
    for row in rows:
        counts[_backfill_one(row, file_repo, hash_audio)] += 1
    return HashBackfillBatch(
        hashed=counts[BackfillOutcome.HASHED],
        changed=counts[BackfillOutcome.CHANGED],
        unreadable=counts[BackfillOutcome.UNREADABLE],
        last_path=rows[-1].file_path if rows else None,
    )
