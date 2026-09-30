"""The internal session API Liquidsoap calls (spec: the contract: X-Session-Token; D24: the
loopback check follows the bind address; 200 item, 410 at the end)."""

from __future__ import annotations

from ipaddress import IPv4Address
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from backend.config import BindHost
from backend.dependencies import require_internal_client
from backend.routers import stream_internal
from backend.services.streaming.service import StreamService
from tests.services.streaming.helpers import DAY, STATION, Rig, make_rig, song

LOOPBACK = ("127.0.0.1", 50123)
LOOPBACK_BIND: BindHost = IPv4Address("127.0.0.1")


def app_for(service: StreamService | None, bind: BindHost = LOOPBACK_BIND) -> FastAPI:
    app = FastAPI()
    app.include_router(stream_internal.router)
    app.state.stream_service = service
    app.state.server_host = bind
    return app


async def send(
    app: FastAPI,
    method: str,
    url: str,
    token: str | None,
    client: tuple[str, int] = LOOPBACK,
) -> httpx.Response:
    headers = {} if token is None else {"X-Session-Token": token}
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.request(method, url, headers=headers)


def url(session_id: str, seq: int, report: str = "") -> str:
    return f"/internal/stream/sessions/{session_id}/items/{seq}{report}"


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    rig = make_rig(tmp_path)
    rig.schedule.set_day(STATION, DAY, [song("06:00:00"), song("06:03:20")])
    return rig


async def test_liquidsoap_gets_the_item_as_json(rig: Rig) -> None:
    sid = await rig.open()
    response = await send(app_for(rig.service), "GET", url(sid, 0), rig.engines.token(sid))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"path", "annotations"}
    assert body["annotations"]["item_seq"] == "0"
    assert body["annotations"]["liq_cue_in"] == "60.000"


@pytest.mark.parametrize("host", ["192.168.1.20", "10.0.0.5"])
async def test_other_machines_are_refused(rig: Rig, host: str) -> None:
    sid = await rig.open()
    response = await send(
        app_for(rig.service), "GET", url(sid, 0), rig.engines.token(sid), client=(host, 50123)
    )
    assert response.status_code == 403


@pytest.mark.parametrize("report", ["/started", "/failed"])
async def test_reports_from_other_machines_are_refused(rig: Rig, report: str) -> None:
    sid = await rig.open()
    token = rig.engines.token(sid)
    await send(app_for(rig.service), "GET", url(sid, 0), token)
    lan = ("192.168.1.20", 50123)
    response = await send(app_for(rig.service), "POST", url(sid, 0, report), token, client=lan)
    assert response.status_code == 403


async def test_a_lan_bound_api_accepts_its_own_address(rig: Rig) -> None:
    sid = await rig.open()
    app = app_for(rig.service, IPv4Address("192.168.1.20"))
    own = ("192.168.1.20", 50123)
    response = await send(app, "GET", url(sid, 0), rig.engines.token(sid), client=own)
    assert response.status_code == 200


async def test_a_wrong_or_missing_token_is_forbidden(rig: Rig) -> None:
    sid = await rig.open()
    wrong = await send(app_for(rig.service), "GET", url(sid, 0), "nope")
    missing = await send(app_for(rig.service), "GET", url(sid, 0), None)
    assert (wrong.status_code, missing.status_code) == (403, 403)


async def test_an_unknown_session_is_not_found(rig: Rig) -> None:
    sid = await rig.open()
    token = rig.engines.token(sid)
    response = await send(app_for(rig.service), "GET", url("no-such-session", 0), token)
    assert response.status_code == 404


async def test_after_the_last_item_the_answer_is_410(rig: Rig) -> None:
    sid = await rig.open()
    token = rig.engines.token(sid)
    app = app_for(rig.service)
    songs = [(await send(app, "GET", url(sid, seq), token)).status_code for seq in (0, 1)]
    after = await send(app, "GET", url(sid, 2), token)
    assert songs == [200, 200]
    assert after.status_code == 410


async def test_a_seq_ahead_is_retried_not_ended(rig: Rig) -> None:
    # Contract: "410 = end of schedule ...; any other status = retry later".
    sid = await rig.open()
    ahead = await send(app_for(rig.service), "GET", url(sid, 1), rig.engines.token(sid))
    assert ahead.status_code not in {200, 410}


async def test_started_and_failed_reports_are_accepted(rig: Rig) -> None:
    sid = await rig.open()
    token = rig.engines.token(sid)
    app = app_for(rig.service)
    await send(app, "GET", url(sid, 0), token)
    started = await send(app, "POST", url(sid, 0, "/started"), token)
    failed = await send(app, "POST", url(sid, 0, "/failed"), token)
    assert (started.status_code, failed.status_code) == (204, 204)


async def test_with_streaming_off_the_answer_is_503() -> None:
    response = await send(app_for(None), "GET", url("any", 0), "x")
    assert response.status_code == 503


def test_main_mounts_the_internal_api_behind_the_client_check() -> None:
    from backend.main import app

    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    route = routes["/internal/stream/sessions/{session_id}/items/{seq}"]
    assert require_internal_client in [d.call for d in route.dependant.dependencies]
