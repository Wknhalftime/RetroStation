"""The targeted re-check of undecided matching: its wave, its rewind, its fan-out set.

AUD-R022 D1/D2, spec 2026-10-05 §4.2. Only needs_review and auto_rejected go back to pending;
auto_matched, manual_matched and manual_rejected are never touched.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from backend.domain.enums import RecheckScope
from backend.repositories.artist_linking import ArtistLinkingRepository
from backend.repositories.broadcast_artists import BroadcastArtistRepository
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.library_files import LibraryFileRepository


@dataclass(frozen=True)
class RewindCounts:
    """How many undecided artists and songs one rewind returned to pending."""

    artists: int
    songs: int


def recheck_names(
    scope: RecheckScope,
    watermark: datetime | None,
    library_files: LibraryFileRepository,
    artist_linking: ArtistLinkingRepository,
) -> set[str] | None:
    """The normalized artist names in this re-check's wave; None means every undecided item.

    Scope ALL (the button) and the first run (no watermark) re-check everything. Otherwise the
    wave is the artists of files indexed or gone missing after the watermark, plus the artists
    linked to a MusicBrainz ID after it (AUD-R026, D15), so their undecided songs are re-scored
    against the new MBID, even when a later run linked them.
    """
    if scope is RecheckScope.ALL or watermark is None:
        return None
    changed = library_files.normalized_artist_names_changed_since(watermark)
    return changed | artist_linking.normalized_names_linked_since(watermark)


def rewind_wave(
    names: Collection[str] | None,
    artists: BroadcastArtistRepository,
    songs: BroadcastTrackIdentityRepository,
) -> RewindCounts:
    """Return the wave's undecided artists, then their undecided songs, to pending.

    Names map to artist ids first, then artists rewind, then songs. Status only: match rows
    stay, so the review screen keeps the old suggestion until a new result replaces it. None
    means everything; an empty collection means nothing.
    """
    artist_ids = None if names is None else artists.ids_by_normalized_names(names)
    rewound_artists = artists.rewind_undecided(names)
    rewound_songs = songs.rewind_undecided(artist_ids)
    return RewindCounts(artists=rewound_artists, songs=rewound_songs)


def playlists_with_pending_work(
    artists: BroadcastArtistRepository, songs: BroadcastTrackIdentityRepository
) -> set[UUID]:
    """Every playlist with a pending artist or song, rewound now or left by an earlier run."""
    return artists.playlist_ids_with_pending() | songs.playlist_ids_with_pending()
