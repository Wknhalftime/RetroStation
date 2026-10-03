"""Malformed and abandoned sign-off uploads are never server errors (PR G1, Task 3 review fix).

Requirements: house rule "never skip error handling"; D26 and the plan's route table (every
422 that G raises uses FastAPI's list shape). python-multipart raises ``MultipartParseError``
for a body it cannot parse (garbage, a wrong boundary, a boundary line without CR, a broken
part header), which Starlette's parser does not catch; a client that goes away mid-upload
makes ``request.stream()`` raise ``ClientDisconnect``. Neither may answer 500, store anything
or leave a spooled temporary file open.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import starlette.formparsers
from fastapi import FastAPI
from structlog.testing import capture_logs

from backend.dependencies import get_current_token, get_sign_off_ports
from backend.domain.streaming import ProbedClip
from backend.routers.v1 import router as v1_router
from backend.services.audio_tags import probe_clip
from backend.services.streaming.sign_off import SignOffPorts
from tests.fakes.user_settings import FakeUserSettingRepository

SIGN_OFF = "/api/v1/streaming/sign-off"
KEY = "stream_sign_off"
BOUNDARY = "g1-malformed"
type Spooled = tempfile.SpooledTemporaryFile[bytes]
HEAD = (
    f"--{BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n'
    "Content-Type: application/octet-stream\r\n\r\n"
).encode()


@dataclass
class World:
    """A settings fake, a sign-off folder and the real probe (recorded)."""

    folder: Path
    settings: FakeUserSettingRepository = field(default_factory=FakeUserSettingRepository)
    probed: list[Path] = field(default_factory=list)

    def probe(self, path: Path) -> ProbedClip:
        self.probed.append(path)
        return probe_clip(path)

    def app(self) -> FastAPI:
        app = FastAPI()
        app.include_router(v1_router)
        ports = SignOffPorts(
            settings=self.settings, folder=self.folder, probe=self.probe, commit=lambda: None
        )
        app.dependency_overrides[get_sign_off_ports] = lambda: ports
        app.dependency_overrides[get_current_token] = lambda: "test-token"
        return app

    def assert_nothing_stored(self) -> None:
        assert self.settings.get(KEY) is None
        assert self.probed == []
        files = sorted(p.name for p in self.folder.iterdir()) if self.folder.exists() else []
        assert files == []


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "sign-off")


@pytest.fixture
def spooled(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[Spooled]]:
    """Every temporary file Starlette's multipart parser spools a file part into."""
    made: list[Spooled] = []

    class Recording(tempfile.SpooledTemporaryFile[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", Recording)
    yield made


MALFORMED = {
    "garbage": b"this is not a multipart body at all",
    "wrong-boundary": HEAD.replace(BOUNDARY.encode(), b"other-boundary")
    + b"RIFF....\r\n--other-boundary--\r\n",
    "boundary-without-cr": HEAD.replace(b"\r\n", b"\n") + b"RIFF....\n--" + BOUNDARY.encode(),
    "broken-header-after-a-file-part": HEAD
    + bytes(5_000)
    + f"\r\n--{BOUNDARY}\r\nnot a header line\r\n\r\n".encode(),
}


@pytest.mark.parametrize("case", list(MALFORMED))
async def test_a_malformed_multipart_body_is_422_in_fastapis_shape(
    world: World, spooled: list[Spooled], case: str
) -> None:
    headers = {"content-type": f"multipart/form-data; boundary={BOUNDARY}"}
    transport = httpx.ASGITransport(app=world.app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        response = await http.post(SIGN_OFF, content=MALFORMED[case], headers=headers)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail
    assert all({"loc", "msg", "type"} <= set(entry) for entry in detail)
    if case == "broken-header-after-a-file-part":
        assert spooled  # the clip's part had begun spooling when the parse failed
    assert all(file.closed for file in spooled)
    world.assert_nothing_stored()


async def test_a_client_that_disconnects_mid_upload_is_not_a_server_error(
    world: World, spooled: list[Spooled]
) -> None:
    # The client sends the part header and some of the clip, then goes away.
    incoming: list[dict[str, Any]] = [
        {"type": "http.request", "body": HEAD + bytes(4_096), "more_body": True},
    ]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": SIGN_OFF,
        "raw_path": SIGN_OFF.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", f"multipart/form-data; boundary={BOUNDARY}".encode()),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    with capture_logs() as logs:
        await world.app()(scope, receive, send)
    [start] = [m for m in sent if m["type"] == "http.response.start"]
    assert start["status"] == 400
    aborted = [e for e in logs if e["event"] == "stream_sign_off_upload_aborted"]
    assert aborted and aborted[0]["log_level"] in ("info", "warning")
    assert spooled  # the clip's part had begun spooling when the client left
    assert all(file.closed for file in spooled)
    world.assert_nothing_stored()
