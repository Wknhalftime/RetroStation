"""A failed stream read on the engine's item route is a 503, not a 500 (G1 review, P2).

Requirements: D88 (a read that fails is answered with an error, and the engine retries); the
contract (any status other than 200 and 410 tells the engine to retry); listen.py's table
(``StreamReadError`` → 503 ``unavailable``). The sign-off read at the end of the schedule is
made to fail once; the retry then gets the clip.
"""

from __future__ import annotations

from ipaddress import IPv4Address
from pathlib import Path

import httpx
from fastapi import FastAPI

from backend.config import BindHost
from backend.domain.streaming import ClipFormat, SignOff, StreamReadError
from backend.routers import stream_internal
from tests.services.streaming.sign_off_rig import make_sign_off_rig, morning

LOOPBACK = ("127.0.0.1", 50123)
LOOPBACK_BIND: BindHost = IPv4Address("127.0.0.1")
CLIP = SignOff(
    file_name="0123456789abcdef.mp3", format=ClipFormat.MP3, span_ms=12_500, name="Good night.mp3"
)
CLIP_SEQ = 3


async def test_a_failed_read_at_the_end_is_503_unavailable_then_the_retry_plays(
    tmp_path: Path,
) -> None:
    rig = make_sign_off_rig(tmp_path, clip=CLIP)
    morning(rig)
    sid = await rig.open()
    for seq in range(CLIP_SEQ):
        await rig.item(sid, seq)
        rig.started(sid, seq)
    app = FastAPI()
    app.include_router(stream_internal.router)
    app.state.stream_service = rig.service
    app.state.server_host = LOOPBACK_BIND
    headers = {"X-Session-Token": rig.engines.token(sid)}
    item = f"/internal/stream/sessions/{sid}/items/{CLIP_SEQ}"
    transport = httpx.ASGITransport(app=app, client=LOOPBACK, raise_app_exceptions=False)
    rig.fail_next_read(StreamReadError("stream_sign_off: canceling statement due to lock timeout"))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        failed = await http.get(item, headers=headers)
        retried = await http.get(item, headers=headers)
    assert failed.status_code == 503
    assert failed.json() == {"detail": "unavailable"}
    assert (retried.status_code, retried.json()["path"]) == (200, rig.clip_path(CLIP))
