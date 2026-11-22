"""The panel's MusicBrainz artist search (phase 4.1 S6; design C46; AUD-R042, AUD-R090).

``GET /api/v1/matching/mb-artists`` is the one API route that calls MusicBrainz. Routers catch
domain errors only (``.claude/rules/architecture-boundaries.md``), so the translation of the
client's httpx error into ``MusicBrainzUnavailableError`` lives here; the route answers 503.
"""

from __future__ import annotations

import httpx

from backend.domain.matching import MusicBrainzUnavailableError
from backend.services.mb_client import MusicBrainzClientProtocol
from backend.services.mb_types import MbArtistResult


def search_artists_by_name(client: MusicBrainzClientProtocol, query: str) -> list[MbArtistResult]:
    """The client's artist search; any MusicBrainz error (after the client's own retries)
    becomes ``MusicBrainzUnavailableError``."""
    try:
        return client.search_artist(query)
    except httpx.HTTPError as exc:
        raise MusicBrainzUnavailableError(f"MusicBrainz artist search failed: {exc}") from exc
