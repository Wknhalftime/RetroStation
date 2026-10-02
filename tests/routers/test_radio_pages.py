"""The public listening pages (PR F2; plan-f2.md Task 1, module P; traceability-f2.md).

Spec D71: ``/radio`` and ``/radio/{call}/{year}`` are public backend pages, outside
``/api/v1``, with no ``X-Airwave-Token``, reachable from the LAN and showing no secrets.
D75: the pages are plain JavaScript served as they are. Design question 1: an allowlisted asset
route at ``/static/radio/{name}``, ``no-cache``, ``nosniff``, a same-origin CSP and no inline
code. P13–P15 scan the page scripts' source (the frontend has no ``@types/node``); Task 0 proves
each scan fails on a planted violation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from html.parser import HTMLParser
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from backend.dependencies import get_current_token, get_station_year_repos
from backend.routers import radio, radio_pages
from backend.services.streaming.station_years import StationYearRepos
from tests.fakes.broadcast_days import FakeBroadcastDayRepository
from tests.fakes.broadcast_stations import FakeBroadcastStationRepository

PAGES = ("/radio", "/radio/KIOA/1995")
PAGE_DIR = Path(__file__).resolve().parents[2] / "backend" / "web" / "radio"
ASSET_PREFIX = "/static/radio/"
ASSET_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
HTML = "text/html; charset=utf-8"
# What the pages need from their own origin: module scripts, the stylesheet, fetch and
# EventSource, and the audio stream.
NEEDED_SOURCES = ("script-src", "style-src", "connect-src", "media-src")
FRONTEND_DIR = PAGE_DIR.parents[2] / "frontend"
SECRETS = ("dev-token", "airwave", "127.0.0.1", "localhost", "http://", "https://", "/api/v1")
# Module specifiers: static `import … from "x"`, `export … from "x"`, bare `import "x"`, and
# `import("x")` (dynamic, or a JSDoc type import).
IMPORT_SPECIFIER = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)["']([^"']+)["']""")


def app_for() -> FastAPI:
    app = FastAPI()
    app.include_router(radio_pages.router)
    app.state.stream_service = None  # streaming off (D34)
    return app


async def get(app: FastAPI, path: str) -> httpx.Response:
    """A LAN client with no token (D71)."""
    transport = httpx.ASGITransport(app=app, client=("192.168.1.30", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.get(path)


class _Tags(HTMLParser):
    """Every start tag of a page, with its attributes, and whether each script has a body."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_bodies: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        if tag == "script":
            self._in_script = True
            self.script_bodies.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self.script_bodies[-1] += data


def tags_of(html: str) -> _Tags:
    parser = _Tags()
    parser.feed(html)
    parser.close()
    return parser


def assets_named_by(html: str) -> list[str]:
    """The `/static/radio/` paths a page names in a `src` or an `href`."""
    found: list[str] = []
    for _, attrs in tags_of(html).tags:
        for name in ("src", "href"):
            value = attrs.get(name)
            if value is not None and value.startswith(ASSET_PREFIX):
                found.append(value)
    return found


def scripts_named_by(html: str) -> list[str]:
    return [
        attrs["src"] or ""
        for tag, attrs in tags_of(html).tags
        if tag == "script" and attrs.get("src") is not None
    ]


def sibling_imports(source: str) -> list[str]:
    """The asset paths a script imports as `./x.js` siblings."""
    return [
        ASSET_PREFIX + spec.removeprefix("./")
        for spec in IMPORT_SPECIFIER.findall(source)
        if spec.startswith("./")
    ]


async def crawl(app: FastAPI) -> dict[str, httpx.Response]:
    """Every asset reachable from the two pages: their `src`/`href`, then each script's imports."""
    queue: list[str] = []
    for page in PAGES:
        queue.extend(assets_named_by((await get(app, page)).text))
    seen: dict[str, httpx.Response] = {}
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        response = await get(app, path)
        seen[path] = response
        if path.endswith(".js") and response.status_code == 200:
            queue.extend(sibling_imports(response.text))
    return seen


def page_scripts() -> list[Path]:
    scripts = sorted(PAGE_DIR.glob("*.js"))
    assert scripts, f"no page scripts in {PAGE_DIR}"
    return scripts


def jsdoc_comments(source: str) -> Iterator[str]:
    """Each `/** … */` comment, with every line's leading `*` decoration removed."""
    for match in re.finditer(r"/\*\*(.*?)\*/", source, flags=re.DOTALL):
        yield "\n".join(re.sub(r"^\s*\*?", "", line) for line in match.group(1).splitlines())


def tag_types(comment: str) -> Iterator[str]:
    """The `{…}` type of each `@type`, `@param`, `@returns`, `@typedef`, `@property` or
    `@template` tag, with nested braces balanced. Prose outside the braces is not returned."""
    tags = r"@(?:type|param|returns?|typedef|property|prop|template)\b\s*\{"
    for match in re.finditer(tags, comment):
        depth, start = 1, match.end()
        index = start
        while index < len(comment) and depth:
            depth += {"{": 1, "}": -1}.get(comment[index], 0)
            index += 1
        yield comment[start : index - 1]


def loose_types(type_text: str) -> list[str]:
    """`any`, `*`, `?`, `Object` or `Function` standing as a type (whole tokens), outside
    string literals. A `?` that marks an optional property or parameter (`name?:`) is allowed."""
    text = re.sub(r"""(["'`])(?:\\.|(?!\1).)*\1""", '""', type_text)
    found = re.findall(r"\b(?:any|Object|Function)\b", text)
    found += re.findall(r"\*", text)
    found += re.findall(r"\?(?!\s*:)", text)
    return found


async def test_the_station_list_page_is_html_for_a_lan_client_without_a_token() -> None:
    """P1 — D71."""
    response = await get(app_for(), "/radio")
    assert response.status_code == 200
    assert response.headers["content-type"] == HTML


@pytest.mark.parametrize("path", ["/radio/KIOA/1995", "/radio/kioa/1995", "/radio/KXXX/2001"])
async def test_the_player_page_is_html_for_any_station_year(path: str) -> None:
    """P2 — D71; D72 (any case); contract §2. The page reads its station-year from its own URL,
    so any call letters get the player, whose page holds the one `<audio>` element."""
    response = await get(app_for(), path)
    assert response.status_code == 200
    assert response.headers["content-type"] == HTML
    assert [tag for tag, _ in tags_of(response.text).tags].count("audio") == 1


@pytest.mark.parametrize("year", ["0", "10000", "nineteen"])
async def test_a_player_path_whose_year_is_not_a_calendar_year_is_422(year: str) -> None:
    """P3 — contract §2 (the year is validated as `/listen` validates it)."""
    response = await get(app_for(), f"/radio/KIOA/{year}")
    assert response.status_code == 422


async def test_every_script_and_stylesheet_a_page_names_is_served_with_its_type() -> None:
    """P4 — D75 (served as they are; explicit types, since Windows may map `.js` to text/plain
    and a module script with `nosniff` and the wrong type is refused, R9)."""
    app = app_for()
    for page in PAGES:
        assert scripts_named_by((await get(app, page)).text), f"{page} names no script"
    assets = await crawl(app)
    assert any(path.endswith(".css") for path in assets)
    for path, response in assets.items():
        assert response.status_code == 200, path
        assert response.headers["content-type"] == ASSET_TYPES.get(Path(path).suffix), path


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("nope.js", None),
        ("list.html", None),
        ("..%5C..%5Cmain.py", "../../main.py"),
        ("..%2F..%2Fmain.py", "../../main.py"),
        ("..%5C..%5C..%5Cpyproject.toml", "../../../pyproject.toml"),
        ("%2E%2E%2F%2E%2E%2F%2E%2E%2Fpyproject.toml", "../../../pyproject.toml"),
    ],
)
async def test_a_name_outside_the_page_folder_is_404(name: str, target: str | None) -> None:
    """P5 — D71 (no secrets); review focus 7 (traversal on the asset route). Each traversal
    probe aims at a file that really exists outside the page folder (`backend/main.py`, the
    repo's `pyproject.toml`), so a server that joins the name to the folder would answer 200
    (audit MF2)."""
    if target is not None:
        assert (PAGE_DIR / target).resolve().is_file(), f"the probe's target {target} is gone"
    response = await get(app_for(), ASSET_PREFIX + name)
    assert response.status_code == 404


async def test_pages_and_scripts_are_revalidated_and_never_sniffed() -> None:
    """P6 — design question 1 (`no-cache`: no build step, so no content hash in the names)."""
    app = app_for()
    responses = [await get(app, page) for page in PAGES]
    responses += list((await crawl(app)).values())
    assert len(responses) > len(PAGES)
    for response in responses:
        assert response.headers.get("cache-control") == "no-cache", response.url
        assert response.headers.get("x-content-type-options") == "nosniff", response.url


async def test_the_pages_allow_only_their_own_origin() -> None:
    """P7 — D71; design question 1. The CSP allows nothing but the page's own origin:
    `default-src 'none'`; the scripts, the stylesheet, the fetch and EventSource connections and
    the audio are allowed from `'self'`; every other directive allows only `'self'` or `'none'`.
    A stricter correct policy passes (audit N3). `no-referrer` keeps the listener key out of
    `Referer` headers."""
    app = app_for()
    for page in PAGES:
        response = await get(app, page)
        directives: dict[str, list[str]] = {}
        for part in response.headers.get("content-security-policy", "").split(";"):
            words = part.split()
            if words:
                directives[words[0].lower()] = words[1:]
        assert directives.get("default-src") == ["'none'"], page
        for needed in NEEDED_SOURCES:
            assert directives.get(needed) == ["'self'"], (page, needed)
        for name, sources in directives.items():
            assert sources, (page, name)
            assert set(sources) <= {"'self'", "'none'"}, (page, name, sources)
        assert response.headers.get("referrer-policy") == "no-referrer", page


async def test_the_pages_have_no_inline_script_style_or_handler() -> None:
    """P8 — design question 1 (the CSP holds: nothing inline)."""
    app = app_for()
    for page in PAGES:
        parsed = tags_of((await get(app, page)).text)
        for tag, attrs in parsed.tags:
            assert tag != "style", page
            assert "style" not in attrs, (page, tag)
            assert not [name for name in attrs if name.startswith("on")], (page, tag)
            if tag == "script":
                assert attrs.get("src"), page
        assert all(not body.strip() for body in parsed.script_bodies), page


async def test_no_served_file_holds_a_secret_or_a_fixed_host() -> None:
    """P9 — D71 (no secrets); the contract (relative, same-origin URLs only)."""
    app = app_for()
    bodies = {page: (await get(app, page)).text for page in PAGES}
    bodies |= {path: response.text for path, response in (await crawl(app)).items()}
    files = [path for path in PAGE_DIR.iterdir() if path.is_file()]
    bodies |= {str(path): path.read_text(encoding="utf-8") for path in files}
    for where, body in bodies.items():
        for secret in SECRETS:
            assert secret not in body.lower(), (where, secret)


async def test_the_pages_are_served_while_streaming_is_off() -> None:
    """P10 — D71; D34 (streaming off does not gate the pages)."""
    app = app_for()
    assert app.state.stream_service is None
    for page in PAGES:
        assert (await get(app, page)).status_code == 200
    scripts = scripts_named_by((await get(app, "/radio")).text)
    assert scripts, "the list page names no script"
    assert (await get(app, scripts[0])).status_code == 200


async def test_the_list_data_and_the_pages_share_the_radio_prefix() -> None:
    """P11 — D71; contract notes (routing: `/radio/station-years` has two segments, the player
    three). The page router is included first, the order most likely to shadow the data."""
    repos = StationYearRepos(
        stations=FakeBroadcastStationRepository(), days=FakeBroadcastDayRepository()
    )
    app = app_for()
    app.include_router(radio.router)
    app.dependency_overrides[get_station_year_repos] = lambda: repos
    data = await get(app, "/radio/station-years")
    assert (data.status_code, data.json()) == (200, [])
    assert data.headers["content-type"] == "application/json"
    for page in PAGES:
        response = await get(app, page)
        assert (response.status_code, response.headers["content-type"]) == (200, HTML)


def test_main_serves_the_pages_and_assets_at_the_root_without_the_api_token() -> None:
    """P12 — D71; the composition root (`main.py` is the only wiring site)."""
    from backend.main import app

    def calls(dependant: Dependant) -> list[Callable[..., object] | None]:
        found: list[Callable[..., object] | None] = [dependant.call]
        for sub in dependant.dependencies:
            found.extend(calls(sub))
        return found

    routes = {route.path: route for route in app.routes if isinstance(route, APIRoute)}
    for path in ("/radio", "/radio/{call_letters}/{year}", "/static/radio/{name}"):
        assert path in routes, path
        assert "GET" in routes[path].methods, path
        assert get_current_token not in calls(routes[path].dependant), path


def test_the_page_scripts_never_write_html_or_run_strings() -> None:
    """P13 — XSS (server strings reach the page through `textContent` only)."""
    forbidden = ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write")
    for script in page_scripts():
        source = script.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in source, (script.name, name)
        assert not re.search(r"\beval\s*\(", source), script.name
        assert not re.search(r"\bnew\s+Function\b", source), script.name


def test_the_page_scripts_keep_type_checking_on_and_use_no_any() -> None:
    """P14 — D75 (checked by `tsc --checkJs`); CLAUDE.md ("never use any"); review I5.

    No `@ts-nocheck`, `@ts-ignore` or `@ts-expect-error` anywhere. Inside the braces of a JSDoc
    `@type`, `@param`, `@returns`, `@typedef`, `@property` or `@template` tag, none of `any`,
    `*`, `?`, `Object` or `Function` stands as a type (prose outside the braces is ignored).
    """
    for script in page_scripts():
        source = script.read_text(encoding="utf-8")
        for pragma in ("@ts-nocheck", "@ts-ignore", "@ts-expect-error"):
            assert pragma not in source, (script.name, pragma)
        for comment in jsdoc_comments(source):
            for type_text in tag_types(comment):
                assert not loose_types(type_text), (script.name, type_text)


def test_the_page_scripts_import_only_their_siblings_and_need_no_secure_context() -> None:
    """P15 — D75 (each import is a served sibling); D71 (a phone on plain-http LAN is not a
    secure context, so no `crypto.randomUUID`)."""
    sibling = re.compile(r"\./[A-Za-z0-9_-]+\.js")
    for script in page_scripts():
        source = script.read_text(encoding="utf-8")
        for spec in IMPORT_SPECIFIER.findall(source):
            assert sibling.fullmatch(spec), (script.name, spec)
        assert "randomUUID" not in source, script.name


def test_the_page_scripts_stay_in_the_frontend_type_check() -> None:
    """P16 — D75 ("type-checked with the frontend's tools"; audit SF9). `npm run typecheck`
    runs `tsc -p tsconfig.radio.json`, which checks the page scripts (`checkJs`) and the radio
    tests."""
    package = json.loads((FRONTEND_DIR / "package.json").read_text(encoding="utf-8"))
    script = package["scripts"]["typecheck"]
    assert re.search(r"\btsc\b[^&|;]*-p\s+tsconfig\.radio\.json", script), script
    config = json.loads((FRONTEND_DIR / "tsconfig.radio.json").read_text(encoding="utf-8"))
    options = config.get("compilerOptions", {})
    assert options.get("checkJs") is True
    assert options.get("allowJs") is True
    assert options.get("noEmit") is True
    assert "../backend/web/radio/**/*.js" in config.get("include", [])
    assert "radio/**/*.ts" in config.get("include", [])
