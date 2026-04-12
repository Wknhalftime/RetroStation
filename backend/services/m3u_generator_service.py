"""M3U playlist export service.

Resolves the preferred audio file for each play event using the priority chain
defined in the design spec (Section 3.4):

    format_override > song_master > direct match (match.library_file_id)

Only events whose identity has a match_status of AUTO_MATCHED or MANUAL_MATCHED
are emitted; all others are silently skipped.
"""

from __future__ import annotations

from uuid import UUID

from backend.domain.broadcast import BroadcastPlayEvent
from backend.domain.enums import MatchStatus
from backend.repositories.format_overrides import FormatOverrideRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.matches import MatchRepository
from backend.repositories.recordings import RecordingRepository
from backend.repositories.song_masters import SongMasterRepository
from backend.repositories.track_identities import BroadcastTrackIdentityRepository
from backend.repositories.user_settings import UserSettingRepository

_MATCHED_STATUSES: frozenset[MatchStatus] = frozenset(
    {MatchStatus.AUTO_MATCHED, MatchStatus.MANUAL_MATCHED}
)


def generate_m3u(
    *,
    events: list[BroadcastPlayEvent],
    identity_repo: BroadcastTrackIdentityRepository,
    match_repo: MatchRepository,
    file_repo: LibraryFileRepository,
    recording_repo: RecordingRepository,
    master_repo: SongMasterRepository,
    override_repo: FormatOverrideRepository,
    settings_repo: UserSettingRepository,
    station_format: str | None = None,
) -> str:
    """Generate an M3U playlist string for the given events.

    Args:
        events: Pre-fetched list of play events to export.
        identity_repo: Repository for log identities.
        match_repo: Repository for identity matches.
        file_repo: Repository for library files.
        recording_repo: Repository for MusicBrainz recordings.
        master_repo: Repository for song masters.
        override_repo: Repository for format overrides.
        settings_repo: Repository for user settings.
        station_format: Optional station format string used for format_override
            lookup (e.g. ``"CHR"``).

    Returns:
        A UTF-8 M3U string beginning with ``#EXTM3U``.
    """
    _local = settings_repo.get("local_path_prefix")
    local_prefix: str = _local.value if _local is not None else ""
    _navidrome = settings_repo.get("navidrome_path_prefix")
    navidrome_prefix: str = _navidrome.value if _navidrome is not None else ""

    sorted_events = sorted(events, key=lambda e: e.played_at)

    lines: list[str] = ["#EXTM3U"]

    for event in sorted_events:
        identity = identity_repo.get_by_id(event.identity_id)
        if identity is None or identity.match_status not in _MATCHED_STATUSES:
            continue

        match = match_repo.get_by_identity(identity.id)
        if match is None or match.library_file_id is None:
            continue

        resolved_file_id: UUID = match.library_file_id

        # Attempt to walk up to a work so we can check master / format override.
        direct_file = file_repo.get_by_id(match.library_file_id)
        if direct_file is not None and direct_file.recording_id is not None:
            recording = recording_repo.get_by_id(direct_file.recording_id)
            if recording is not None and recording.work_id is not None:
                work_id: str = recording.work_id

                # Priority 1 (lowest): song_master
                master = master_repo.get_by_work(work_id)
                if master is not None:
                    resolved_file_id = master.preferred_file_id

                # Priority 2 (highest): format_override
                if station_format is not None:
                    override = override_repo.get(work_id, station_format)
                    if override is not None:
                        resolved_file_id = override.preferred_file_id

        resolved_file = file_repo.get_by_id(resolved_file_id)
        if resolved_file is None:
            continue

        file_path = resolved_file.file_path
        if local_prefix and navidrome_prefix and file_path.startswith(local_prefix):
            file_path = navidrome_prefix + file_path[len(local_prefix):]

        duration_secs: int = (
            resolved_file.audio.duration_ms // 1000
            if resolved_file.audio.duration_ms is not None
            else -1
        )
        title: str = identity.original_title

        lines.append(f"#EXTINF:{duration_secs},{title}")
        lines.append(file_path)

    return "\n".join(lines) + "\n"
