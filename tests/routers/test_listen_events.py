"""Now-playing over server-sent events (spec D13 and X1: "GET
/listen/{call_letters}/{year}/events?key= returns now-playing SSE"; "Public routes,
unauthenticated, for LAN players"; D73: artist and title only; D78a: status events;
D78b (provisional): the subscription cap; D14: plain HTTP errors before the stream starts;
D28: no key, no events; D34: streaming off; D27: a bad limit setting; D72: any case)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from backend.dependencies import get_current_token
from backend.routers import listen
from backend.services.streaming.service import StreamService
from tests.routers.sse_drive import SseDrive
from tests.services.streaming.events_rig import EventsRig, make_events_rig
from tests.services.streaming.helpers import DAY, STATION, song

EVENTS = "/listen/KIOA/1995/events"


def app_for(service: StreamService | None) -> FastAPI:
    app = FastAPI()
    app.include_router(listen.router)
    app.state.stream_service = service
    return app


def with_morning(rig: EventsRig) -> EventsRig:
    rig.schedule.set_day(
        STATION,
        DAY,
        [song("06:00:00", title="Fernando", artist="ABBA"), song("06:03:20")],
    )
    return rig


@pytest.fixture
def rig(tmp_path: Path) -> EventsRig:
    return with_morning(make_events_rig(tmp_path))


async def test_events_are_a_server_sent_event_stream(rig: EventsRig) -> None:
    async with SseDrive(app_for(rig.service), EVENTS, "key=car") as drive:
        await drive.wait_started()
        drive.leave()
        result = await drive.result()
    assert result.status == 200
    assert result.headers["content-type"].startswith("text/event-stream")
    assert "no-cache" in result.headers["cache-control"]


async def test_a_title_is_a_now_playing_event_with_the_artist_and_title_only(
    rig: EventsRig,
) -> None:
    async with SseDrive(app_for(rig.service), EVENTS, "key=car") as drive:
        await drive.wait_started()
        sid = await rig.open("car")
        await rig.item(sid, 0)
        rig.started(sid, 0)
        await rig.sleep.wait_pending()
        rig.sleep.release()
        frames = await drive.frames(2)
    assert frames[1] == ("now_playing", {"artist": "ABBA", "title": "Fernando"})
    assert set(frames[1][1]) == {"artist", "title"}


async def test_a_status_is_a_status_event_naming_its_kind(rig: EventsRig) -> None:
    async with SseDrive(app_for(rig.service), EVENTS, "key=car") as drive:
        await drive.wait_started()
        rig.service.close(await rig.open("car"))
        frames = await drive.frames(2)
    assert frames == [("status", {"kind": "tuning"}), ("status", {"kind": "stopped"})]


@pytest.mark.parametrize(
    "query", ["", "key=", f"key={'k' * 129}"], ids=["missing", "empty", "too long"]
)
async def test_a_key_is_required(rig: EventsRig, query: str) -> None:
    async with SseDrive(app_for(rig.service), EVENTS, query) as drive:
        result = await drive.result()
    assert result.status == 422
    assert rig.service.event_channels == 0


async def test_unknown_call_letters_are_404_no_broadcast(rig: EventsRig) -> None:
    async with SseDrive(app_for(rig.service), "/listen/KXXX/1995/events", "key=car") as drive:
        result = await drive.result()
    assert (result.status, result.json) == (404, {"detail": "no broadcast"})
    assert rig.service.event_channels == 0


async def test_any_case_reaches_the_same_channel(rig: EventsRig) -> None:
    async with SseDrive(app_for(rig.service), "/listen/kioa/1995/events", "key=car") as drive:
        await drive.wait_started()
        await rig.open("car", call="KIOA")
        frames = await drive.frames(1)
    assert frames == [("status", {"kind": "tuning"})]


async def test_with_streaming_off_events_are_503_unavailable() -> None:
    async with SseDrive(app_for(None), EVENTS, "key=car") as drive:
        result = await drive.result()
    assert (result.status, result.json) == (503, {"detail": "unavailable"})


@pytest.mark.parametrize("year", ["0", "10000", "nineteen"])
async def test_a_year_that_is_not_a_calendar_year_is_422(rig: EventsRig, year: str) -> None:
    async with SseDrive(app_for(rig.service), f"/listen/KIOA/{year}/events", "key=car") as drive:
        result = await drive.result()
    assert result.status == 422


async def test_a_client_that_leaves_ends_its_subscription(tmp_path: Path) -> None:
    # No leak; D78b: a client that leaves frees its place under the cap (2 x 1 here)
    rig = with_morning(make_events_rig(tmp_path, settings={"stream_max_sessions": "1"}))
    app = app_for(rig.service)
    async with SseDrive(app, EVENTS, "key=a") as first, SseDrive(app, EVENTS, "key=b") as second:
        await first.wait_started()
        await second.wait_started()
        first.leave()
        assert (await first.result()).status == 200
        assert rig.service.event_channels == 1
        async with SseDrive(app, EVENTS, "key=c") as third:
            await third.wait_started()
            third.leave()
            assert (await third.result()).status == 200
        second.leave()
        await second.result()
    assert rig.service.event_channels == 0


async def test_the_subscription_is_in_place_when_the_response_starts(rig: EventsRig) -> None:
    # The F2 ordering contract: open the events, wait for them to open, then the audio
    async with SseDrive(app_for(rig.service), EVENTS, "key=car") as drive:
        await drive.wait_started()
        assert rig.service.event_channels == 1
        await rig.open("car")
        frames = await drive.frames(1)
    assert frames == [("status", {"kind": "tuning"})]


def test_main_serves_events_at_the_root_without_the_api_token() -> None:
    from backend.main import app

    def calls(dependant: Dependant) -> list[Callable[..., object] | None]:
        found: list[Callable[..., object] | None] = [dependant.call]
        for sub in dependant.dependencies:
            found.extend(calls(sub))
        return found

    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    route = routes["/listen/{call_letters}/{year}/events"]
    assert get_current_token not in calls(route.dependant)


@pytest.mark.parametrize(
    ("setting", "detail"),
    [("1", "station busy"), ("0", "unavailable")],
    ids=["over the cap", "bad limit setting"],
)
async def test_a_subscription_refused_before_the_stream_starts_is_503(
    tmp_path: Path, setting: str, detail: str
) -> None:
    # D78b: over 2 x stream_max_sessions subscriptions; D27: a limit that cannot be used
    rig = with_morning(make_events_rig(tmp_path, settings={"stream_max_sessions": setting}))
    app = app_for(rig.service)
    taken = [SseDrive(app, EVENTS, f"key=tab-{n}") for n in range(2 if setting == "1" else 0)]
    try:
        for drive in taken:
            await drive.wait_started()
        async with SseDrive(app, EVENTS, "key=one-more") as refused:
            result = await refused.result()
    finally:
        for drive in taken:
            drive.leave()
            await drive.result()
    assert (result.status, result.json) == (503, {"detail": detail})
