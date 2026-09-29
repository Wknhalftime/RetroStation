"""M3U playlist export service.

Which file plays for each play event is curation's view ``play_file_resolution`` (spec D17):
the override for the station's format, else the song master of the matched file's work, else
no file (D22). The export adds no resolution rules of its own. It writes a play only when
that final file is ``present`` (D21), in the order the caller gives the events.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.domain.broadcast import BroadcastPlayEvent
from backend.domain.curation import PlayFileResolution
from backend.domain.enums import FileStatus
from backend.repositories.broadcast_track_identities import BroadcastTrackIdentityRepository
from backend.repositories.library_files import LibraryFileRepository
from backend.repositories.play_file_resolution import PlayFileResolutionRepository
from backend.repositories.user_settings import UserSettingRepository


@dataclass(frozen=True)
class M3uRepos:
    """The repositories the M3U export reads."""

    track_identities: BroadcastTrackIdentityRepository
    play_file_resolution: PlayFileResolutionRepository
    library_files: LibraryFileRepository
    user_settings: UserSettingRepository


@dataclass(frozen=True)
class _PathRewrite:
    """Maps a local library path to the path Navidrome sees."""

    local_prefix: str
    navidrome_prefix: str

    def apply(self, file_path: str) -> str:
        if self.local_prefix and self.navidrome_prefix and file_path.startswith(self.local_prefix):
            return self.navidrome_prefix + file_path[len(self.local_prefix) :]
        return file_path


def generate_m3u(events: Sequence[BroadcastPlayEvent], repos: M3uRepos) -> str:
    """Generate an M3U playlist string for the given events.

    Args:
        events: Play events to export, in the order they are to be written.
        repos: The repositories the export reads.

    Returns:
        A UTF-8 M3U string beginning with ``#EXTM3U``.
    """
    rewrite = _path_rewrite(repos.user_settings)
    resolutions = repos.play_file_resolution.get_for_plays([event.id for event in events])

    lines: list[str] = ["#EXTM3U"]
    for event in events:
        lines.extend(_entry(event, resolutions[event.id], repos, rewrite))
    return "\n".join(lines) + "\n"


def _path_rewrite(user_settings: UserSettingRepository) -> _PathRewrite:
    local_setting = user_settings.get("local_path_prefix")
    navidrome_setting = user_settings.get("navidrome_path_prefix")
    return _PathRewrite(
        local_prefix=local_setting.value if local_setting is not None else "",
        navidrome_prefix=navidrome_setting.value if navidrome_setting is not None else "",
    )


def _entry(
    event: BroadcastPlayEvent,
    resolution: PlayFileResolution,
    repos: M3uRepos,
    rewrite: _PathRewrite,
) -> list[str]:
    """The EXTINF and path lines for one play, or none when it has no playable file."""
    # A missing or deleted file's path would be a dead entry in the player (D21).
    if resolution.file_id is None or resolution.file_status != FileStatus.PRESENT:
        return []
    identity = repos.track_identities.get_by_id(event.identity_id)
    library_file = repos.library_files.get_by_id(resolution.file_id)
    if identity is None or library_file is None:
        return []
    duration_ms = library_file.audio.duration_ms
    duration_secs = duration_ms // 1000 if duration_ms is not None else -1
    return [
        f"#EXTINF:{duration_secs},{identity.original_title}",
        rewrite.apply(library_file.file_path),
    ]
