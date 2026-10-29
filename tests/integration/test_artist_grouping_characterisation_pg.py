"""Characterisation, artist model phase 0a (SPEC S2): today's grouping and artist upserts.

The scan groups a file by its artist name as written plus its base title, and upserts its
artist by that name alone (``ON CONFLICT (normalized_name)``): an artist MBID tag never makes or
splits an artist. Phase 2a changes this on purpose (C41, M33, AUD-R041). Each test scans tagged
copies of a fixture file and reads the result only through each file's work and the Library's
artist routes (the artist list, and an artist's works list), never ``works.artist_id``:

- a tagged file whose name exists as a name-only artist joins that artist;
- two tagged files with one name and different MBIDs share one artist, and one work per title;
- an untagged file for a name that has an MBID artist joins the MBID artist;
- a tag holding two MBIDs keeps one artist named by the whole text, and the file keeps only
  the first MBID (the Library's file list finds it by that MBID only).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path, PureWindowsPath
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.tasks import artist_linking_tasks
from backend.tasks.huey_app import huey
from tests.fakes.mb_client import FakeMbClient
from tests.integration.missing_file_seed import full_scan, put
from tests.lifespan_client import lifespan_client

type Conn = psycopg.Connection[DictRow]

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="module", autouse=True)
def queue_in_memory() -> Iterator[None]:
    """The hand-offs the writes queue land in memory, never in a worker's queue file (from the
    main checkout, a -n 0 run's queue file is the dev worker's)."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey, "storage", MemoryStorage(huey.name))
    yield
    patch.undo()


ALICE_BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"
ALICE_SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"
LINKED_BAND = "0a000000-0000-4000-8000-0000000000b1"
SHARED_NAME = "0a000000-0000-4000-8000-0000000000b2"
DUET_A = "0a000000-0000-4000-8000-0000000000b3"
DUET_B = "0a000000-0000-4000-8000-0000000000b4"


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; the token is bypassed.

    DATABASE_URL names the test database for the whole module, so the linker task uses it.
    """
    patch = pytest.MonkeyPatch()
    patch.setenv("DATABASE_URL", _migrated_db_url)
    patch.setenv("RETROSTATION_SKIP_BOOT_MIGRATIONS", "1")
    get_settings.cache_clear()
    from backend.dependencies import get_current_token
    from backend.main import app

    async def _token() -> str:
        return "test-token"

    app.dependency_overrides[get_current_token] = _token
    try:
        with lifespan_client(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
        patch.undo()


@pytest.fixture
def client(app_client: TestClient, migrated_db: str) -> TestClient:
    """The module's client; tables are truncated before each test."""
    return app_client


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def tagged(dest: Path, artist: str, title: str, *artist_mbids: str) -> None:
    """A copy of the fixture MP3 tagged with *artist*, *title* and its artist MBIDs, if any.

    Two MBIDs go in one frame as two values, as Picard writes a credit of two artists.
    """
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    if artist_mbids:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=list(artist_mbids)))
    tags.save()


def works_by_file(conn: Conn) -> dict[str, str]:
    """Each file's work, keyed by the file's name."""
    rows = conn.execute("SELECT file_path, work_id FROM library_files").fetchall()
    return {PureWindowsPath(r["file_path"]).name: str(r["work_id"]) for r in rows}


def artists(client: TestClient) -> list[dict[str, Any]]:
    """The Library's artist list: (name, mbid, origin, work count, file count) and the id."""
    response = client.get("/api/v1/library/artists", params={"limit": 500})
    assert response.status_code == 200
    return list(response.json()["items"])


def summary(client: TestClient) -> set[tuple[str, str | None, str, int, int]]:
    return {
        (a["name"], a["mbid"], a["origin"], a["work_count"], a["file_count"])
        for a in artists(client)
    }


def works_listed(client: TestClient, name: str) -> list[tuple[str, str]]:
    """The artist route's works list for the one artist called *name*: (work id, title)."""
    [artist_id] = [a["id"] for a in artists(client) if a["name"] == name]
    response = client.get(f"/api/v1/library/artists/{artist_id}")
    assert response.status_code == 200
    return [(w["id"], w["title"]) for w in response.json()["works"]]


def test_tagged_file_joins_a_name_only_artist(
    conn: Conn, client: TestClient, tmp_path: Path
) -> None:
    """S2: the name exists as a name-only artist; a file tagged with an MBID joins it, and the
    artist keeps no MBID."""
    root = tmp_path / "library"
    tagged(root / "untagged.mp3", "Shared Name", "Morning Light")
    full_scan(conn, root)
    tagged(root / "tagged.mp3", "Shared Name", "Evening Rain", SHARED_NAME)
    full_scan(conn, root)

    work = works_by_file(conn)
    assert summary(client) == {("Shared Name", None, "local", 2, 2)}
    assert works_listed(client, "Shared Name") == [
        (work["tagged.mp3"], "Evening Rain"),
        (work["untagged.mp3"], "Morning Light"),
    ]


def test_one_name_two_mbids_is_one_artist(conn: Conn, client: TestClient, tmp_path: Path) -> None:
    """S2: the Alice Cooper band and singer, told apart only by their MBID tags, are one
    name-only artist; a title both recorded is one work holding both files."""
    root = tmp_path / "library"
    tagged(root / "schools-out.mp3", "Alice Cooper", "School's Out", ALICE_BAND)
    tagged(root / "poison.mp3", "Alice Cooper", "Poison", ALICE_SINGER)
    tagged(root / "wheels-band.mp3", "Alice Cooper", "Under My Wheels", ALICE_BAND)
    tagged(root / "wheels-singer.mp3", "Alice Cooper", "Under My Wheels", ALICE_SINGER)
    full_scan(conn, root)

    work = works_by_file(conn)
    assert work["wheels-band.mp3"] == work["wheels-singer.mp3"]
    assert summary(client) == {("Alice Cooper", None, "local", 3, 4)}
    assert works_listed(client, "Alice Cooper") == [
        (work["poison.mp3"], "Poison"),
        (work["schools-out.mp3"], "School's Out"),
        (work["wheels-band.mp3"], "Under My Wheels"),
    ]


def test_untagged_file_joins_the_mbid_artist_of_its_name(
    conn: Conn, client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S2: the local-artist linker gave Linked Band its MBID (AUD-R026); an untagged file of
    that name joins the MBID artist."""
    root = tmp_path / "library"
    tagged(root / "first.mp3", "Linked Band", "First Song", LINKED_BAND)
    full_scan(conn, root)
    fake = FakeMbClient(
        artists={LINKED_BAND: {"id": LINKED_BAND, "name": "Linked Band", "sort-name": "Band"}}
    )
    monkeypatch.setattr(
        artist_linking_tasks, "MusicBrainzApiClient", lambda *_a, **_k: nullcontext(fake)
    )
    artist_linking_tasks.link_local_artists_task.call_local()
    assert summary(client) == {("Linked Band", LINKED_BAND, "musicbrainz", 1, 1)}

    tagged(root / "second.mp3", "Linked Band", "Second Song")
    full_scan(conn, root)

    work = works_by_file(conn)
    assert summary(client) == {("Linked Band", LINKED_BAND, "musicbrainz", 2, 2)}
    assert works_listed(client, "Linked Band") == [
        (work["first.mp3"], "First Song"),
        (work["second.mp3"], "Second Song"),
    ]


def test_tag_with_two_mbids(conn: Conn, client: TestClient, tmp_path: Path) -> None:
    """S2: a tag holding two MBIDs. The artist is the whole text, never split; the file keeps
    the first MBID only, so the Library's file list finds it by that one."""
    root = tmp_path / "library"
    tagged(root / "duet.mp3", "Duet A & Duet B", "Duet Song", DUET_A, DUET_B)
    full_scan(conn, root)

    work = works_by_file(conn)
    assert summary(client) == {("Duet A & Duet B", None, "local", 1, 1)}
    assert works_listed(client, "Duet A & Duet B") == [(work["duet.mp3"], "Duet Song")]

    def files_tagged(mbid: str) -> list[str]:
        response = client.get("/api/v1/library/files", params={"artist_mbid": mbid})
        assert response.status_code == 200
        return [PureWindowsPath(f["file_path"]).name for f in response.json()["items"]]

    assert (files_tagged(DUET_A), files_tagged(DUET_B)) == (["duet.mp3"], [])
