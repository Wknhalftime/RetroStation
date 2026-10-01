"""Serves the public listening pages and their static assets (spec D71, D75).

D71: ``/radio`` and ``/radio/{call_letters}/{year}`` are public, outside ``/api/v1``, with no
``X-Airwave-Token``, reachable from the LAN. D75: the page files are plain JavaScript, served
byte for byte, with no build step.

Design question 1 (plan-f2.md): an allowlisted asset route (``GET /static/radio/{name}``)
rather than a ``StaticFiles`` mount, so a traversal name can never become a filesystem path
(the name is only ever looked up in ``ASSETS``, never joined onto a directory), and so the
exact media type and cache headers can be set with no templating or subclassing. F1's
``radio.py`` (the JSON station-year list) is left untouched; this router does one thing,
serving the page files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

RADIO_DIR = Path(__file__).resolve().parent.parent / "web" / "radio"

_ASSET_MEDIA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}

ASSETS: dict[str, str] = {
    entry.name: _ASSET_MEDIA_TYPES[entry.suffix]
    for entry in RADIO_DIR.iterdir()
    if entry.is_file() and entry.suffix in _ASSET_MEDIA_TYPES
}

# No content hash in the names (no build step), so a long cache would keep a stale module on
# a phone after an update; `no-cache` makes the browser revalidate every time.
PAGE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "media-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
}

ASSET_HEADERS: dict[str, str] = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

router = APIRouter(include_in_schema=False)


@router.get("/radio")
def get_station_list_page() -> FileResponse:
    """The station-year list page; public, no token (D71). Its data comes from the JSON
    route in ``radio.py`` (``GET /radio/station-years``)."""
    return FileResponse(RADIO_DIR / "list.html", media_type="text/html", headers=PAGE_HEADERS)


@router.get("/radio/{call_letters}/{year}")
def get_player_page(
    call_letters: str,
    year: Annotated[int, PathParam(ge=1, le=9999)],
) -> FileResponse:
    """The player page for any station-year (D71; D72: any case). The page itself reads its
    station-year from ``location.pathname``, so the one page file serves every station-year."""
    return FileResponse(RADIO_DIR / "player.html", media_type="text/html", headers=PAGE_HEADERS)


@router.get("/static/radio/{name}")
def get_radio_asset(name: str) -> FileResponse:
    """One allowlisted script or stylesheet. ``name`` is only ever looked up in ``ASSETS``,
    never joined onto ``RADIO_DIR``, so a traversal name (``..%2F..``) cannot escape it."""
    media_type = ASSETS.get(name)
    if media_type is None:
        raise HTTPException(status_code=404)
    return FileResponse(RADIO_DIR / name, media_type=media_type, headers=ASSET_HEADERS)
