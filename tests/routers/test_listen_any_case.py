"""The public stream in any case (spec D72: "Call letters match in any case"; it replaces the
``/listen/kioa/1995`` case removed from the locked ``test_nothing_to_play_is_404_no_broadcast``)."""

from __future__ import annotations

from pathlib import Path

import httpx
from fastapi import FastAPI

from backend.routers import listen
from tests.services.streaming.helpers import DAY, STATION, make_rig, song

AUDIO = b"\xff\xfb\x90\x00" + bytes(range(256)) * 4


async def test_lowercase_call_letters_play(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.engines.chunks = (AUDIO,)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    app = FastAPI()
    app.include_router(listen.router)
    app.state.stream_service = rig.service
    transport = httpx.ASGITransport(app=app, client=("192.168.1.30", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.get("/listen/kioa/1995")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == AUDIO
