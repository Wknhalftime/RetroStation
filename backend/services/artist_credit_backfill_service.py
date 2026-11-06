"""One batch of the run-once backfill of ``artist_mbids`` and ``credit`` (phase 2a S3, S4; C26,
C69).

Vorbis values come from ``raw_metadata``. Only MP3s are re-read, because a multi-value ID3 frame
lost its separators there. A file holding several MBIDs that its tags can't name asks
``mb_cache`` first, expired or not, then ``lookup_recordings``. An MBID still unnamed then is
stubbed by the sweep, and enrichment names it. A file whose lookup failed waits, unwritten, so no
stub is made for an outage (AUD-R028).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from mutagen._util import MutagenError

from backend.domain.catalog import SPECIAL_PURPOSE_MBIDS, MusicBrainzId, holds_several_mbids
from backend.domain.library import ArtistCreditFile, CreditEntry
from backend.repositories.artist_credit_files import ArtistCreditFilesRepository
from backend.repositories.mb_cache_listing import MusicBrainzCacheListing
from backend.services.artist_credit import (
    credit_from_raw_metadata,
    joins_from_print,
    name_from_musicbrainz,
    tag_values,
)
from backend.services.audio_tags import read_tags
from backend.services.mb_client import MusicBrainzClientProtocol


@dataclass(frozen=True)
class BackfillPorts:
    """What a batch reads and writes through."""

    files: ArtistCreditFilesRepository
    cache: MusicBrainzCacheListing
    client: MusicBrainzClientProtocol


@dataclass
class BackfillTally:
    """What the run did, in the words the PR reports."""

    files: int = 0
    paired: int = 0
    named_from_cache: int = 0
    looked_up: int = 0
    absent: int = 0
    waiting: int = 0


@dataclass(frozen=True)
class _ReadFile:
    """One row with its columns read from tags, before MusicBrainz is asked."""

    file: ArtistCreditFile
    artist: str | None
    mbids: tuple[str, ...]
    credit: tuple[CreditEntry, ...] | None


@dataclass
class _Answers:
    """What ``mb_cache`` and the lookups know for one batch."""

    cached_artists: Mapping[str, str]
    credits: dict[str, list[Mapping[str, object]]] = field(default_factory=dict)
    looked_up: set[str] = field(default_factory=set)


def _read_mp3(file: ArtistCreditFile) -> _ReadFile | None:
    """An MP3's columns from disk; None when it is absent or can't be read (E6)."""
    try:
        audio = read_tags(Path(file.file_path)).audio
    except (OSError, MutagenError):
        return None
    return _ReadFile(file, audio.artist_name, audio.artist_mbids or (), audio.credit)


def _read_saved(file: ArtistCreditFile) -> _ReadFile:
    mbids, credit = credit_from_raw_metadata(file.raw_metadata)
    printed = tag_values(file.raw_metadata.get("artist"))
    return _ReadFile(file, printed[0] if printed else None, mbids, credit)


def _read_files(batch: Sequence[ArtistCreditFile], tally: BackfillTally) -> list[_ReadFile]:
    reads: list[_ReadFile] = []
    for file in batch:
        read = _read_mp3(file) if file.format == "mp3" else _read_saved(file)
        if read is None:
            tally.absent += 1
        else:
            reads.append(read)
    return reads


def _unnamed(entries: Sequence[CreditEntry]) -> list[str]:
    """The MBIDs still wanting a name. A placeholder gets no artist, so it wants none."""
    return [e.mbid for e in entries if e.name is None and e.mbid not in SPECIAL_PURPOSE_MBIDS]


def _recording_to_ask(read: _ReadFile) -> str | None:
    """A recording MBID MusicBrainz could answer for. A malformed one answers 400, which
    ``lookup_recordings`` omits as it does an outage, so the file would wait for good."""
    recording = read.file.recording_mbid
    return recording if recording and MusicBrainzId.parse(recording) else None


def _payload_credit(payload: Mapping[str, object] | None) -> list[Mapping[str, object]] | None:
    credit = payload.get("artist-credit") if payload is not None else None
    return list(credit) if isinstance(credit, list) else None


def _cached_answers(reads: Sequence[_ReadFile], cache: MusicBrainzCacheListing) -> _Answers:
    mbids = {mbid for read in reads for mbid in _unnamed(read.credit or ())}
    recordings = {recording for read in reads if (recording := _recording_to_ask(read))}
    rows = cache.rows(
        [f"artist:{mbid}" for mbid in mbids]
        + [f"recording:{mbid}" for mbid in recordings]
        + [f"recording-by-mbid:{mbid}" for mbid in recordings]
    )
    names = {mbid: rows.get(f"artist:{mbid}", {}).get("name") for mbid in mbids}
    answers = _Answers({mbid: str(name) for mbid, name in names.items() if name})
    for mbid in recordings:
        for prefix in ("recording-by-mbid", "recording"):  # the full row has the joining words
            credit = _payload_credit(rows.get(f"{prefix}:{mbid}"))
            if credit is not None:
                answers.credits[mbid] = credit
    return answers


def _named(read: _ReadFile, answers: _Answers) -> tuple[CreditEntry, ...]:
    recording = _recording_to_ask(read)
    credit = answers.credits.get(recording) if recording else None
    named = name_from_musicbrainz(read.credit or (), answers.cached_artists, credit)
    return joins_from_print(read.artist, named)


def _needs_lookup(read: _ReadFile, answers: _Answers) -> bool:
    return bool(_recording_to_ask(read) and _unnamed(_named(read, answers)))


def _ask_musicbrainz(
    recordings: Sequence[str], answers: _Answers, client: MusicBrainzClientProtocol
) -> None:
    """Look up these recordings, one request each. A failed lookup leaves its key out, and that
    recording's files wait; a 404 answers None, and their names stay null."""
    if not recordings:
        return
    answered = client.lookup_recordings(list(dict.fromkeys(recordings)))
    answers.looked_up.update(answered)
    for recording, payload in answered.items():
        credit = _payload_credit(payload)
        if credit is not None:
            answers.credits[recording] = credit


def _count_several(
    tally: BackfillTally,
    tags_gave: Sequence[CreditEntry],
    written: Sequence[CreditEntry],
    was_looked_up: bool,
) -> None:
    """One written file holding several MBIDs, by where its names came from."""
    tally.files += 1
    if not _unnamed(tags_gave):
        tally.paired += 1
    elif was_looked_up:
        tally.looked_up += 1
    elif not _unnamed(written):
        tally.named_from_cache += 1


def fill_batch(
    batch: Sequence[ArtistCreditFile], ports: BackfillPorts, tally: BackfillTally
) -> None:
    """Fill one batch's columns, in the caller's transaction."""
    reads = _read_files(batch, tally)
    several = [read for read in reads if holds_several_mbids(read.mbids)]
    answers = _cached_answers(several, ports.cache)
    asking = [read for read in several if _needs_lookup(read, answers)]
    _ask_musicbrainz([str(_recording_to_ask(read)) for read in asking], answers, ports.client)
    asked = {read.file.id for read in asking}
    for read in reads:
        if not holds_several_mbids(read.mbids):
            ports.files.write_credit(read.file.id, read.mbids, read.credit)
            tally.files += 1
        elif read.file.id in asked and _recording_to_ask(read) not in answers.looked_up:
            tally.waiting += 1
        else:
            named = _named(read, answers)
            ports.files.write_credit(read.file.id, read.mbids, named)
            _count_several(tally, read.credit or (), named, read.file.id in asked)
