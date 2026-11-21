"""A work's artists are its performances' credits (work merges phase 3b PR 5b, SPEC S7, D42,
D51, D52; scope note R1, R4, R6, R7; AUD-R082, AUD-R088), on PostgreSQL.

``works.artist_id`` is gone (S7, C70). Every reader that asked a work for its artist now reads
the union of its performances' credits, through artist merges where it counts by artist:

- R6: the artist list counts, and an artist's page lists, the works its performances credit,
  a merged member's on its primary's. A work crediting two acts is under both; a work with no
  performance (no file) is under none (D42); a work whose only credit is an MBID no artist
  holds is under none either (D52: the pages stay the credits alone);
- R1: grouping finds a file's work among the works whose performances credit an artist of the
  file's artist name, a MusicBrainz work first on a tie (#258's order); with none it makes a
  local work, and the file's new performance credits the artist grouping picked (D1); a credit
  of an MBID no artist holds is found by the name its files carry, so tagged copies of one song
  share a work (D51, grouping only); a file a writer moves before it held any performance takes
  its new work's default act (a reading);
- R4: the untagged pass searches with the MBID of the file's own performance credit;
- R7: an Unmapped versions item names its work's credited artists, and an artist page's Dismiss
  all takes the works its group's performances credit;
- the work page's detail has no ``artist_id``, and another work no ``artist_name``.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from huey.storage import MemoryStorage
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.db.repositories.library_files import PgLibraryFileRepository
from backend.services.repository_factory import RepositoryFactory, work_curation_repos
from backend.services.work_curation_service import reassign_file
from backend.tasks import huey_app
from tests.integration.missing_file_seed import full_scan
from tests.lifespan_client import lifespan_client
from tests.routers import version_pages_seed as broadcast

pytestmark = pytest.mark.integration

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

type Conn = psycopg.Connection[DictRow]

ROOT = Path(__file__).resolve().parents[2]
AUDIO = ROOT / "tests" / "fixtures" / "audio" / "well_tagged.mp3"
ARTISTS = "/api/v1/library/artists"
NETTLES_MBID = "0a000000-0000-4000-8000-0000000000e1"
BON_JOVI_MBID = "0a000000-0000-4000-8000-0000000000e2"
RATT_MBID = "0a000000-0000-4000-8000-0000000000e4"
UNHELD_MBID = "0a000000-0000-4000-8000-0000000000e5"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; the token is bypassed and
    anything the app queues lands in memory."""
    patch = pytest.MonkeyPatch()
    patch.setattr(huey_app.huey, "storage", MemoryStorage(huey_app.huey.name))
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


# --- seeds and reads ------------------------------------------------------------------------------


def artist(conn: Conn, name: str, mbid: str | None = None) -> str:
    artist_id = f"artist-{name.lower().replace(' ', '-')}"
    conn.execute(
        """INSERT INTO artists (id, name, sort_name, normalized_name, mbid, origin)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (artist_id, name, name, name.lower(), mbid, "musicbrainz" if mbid else "local"),
    )
    return artist_id


def work(conn: Conn, title: str, *, mbid: str | None = None) -> str:
    """A work: no artist of its own since S7."""
    work_id = f"work-{uuid4()}"
    conn.execute(
        "INSERT INTO works (id, title, mbid, origin) VALUES (%s, %s, %s, %s)",
        (work_id, title, mbid, "musicbrainz" if mbid else "local"),
    )
    return work_id


def performance(conn: Conn, work_id: str, *credited: str | None, mbid: str | None = None) -> UUID:
    """A performance of *work_id* crediting *credited* (artist ids) in order; *mbid* gives one
    credit by MBID alone instead."""
    row = conn.execute(
        """INSERT INTO performances (work_id, version_type, length_ms)
           VALUES (%s, 'original', 200000) RETURNING id""",
        (work_id,),
    ).fetchone()
    assert row is not None
    performance_id = UUID(str(row["id"]))
    for position, artist_id in enumerate(credited):
        conn.execute(
            """INSERT INTO performance_credits (performance_id, position, artist_id, role)
               VALUES (%s, %s, %s, 'main')""",
            (performance_id, position, artist_id),
        )
    if mbid is not None:
        conn.execute(
            """INSERT INTO performance_credits (performance_id, position, mbid, role)
               VALUES (%s, 0, %s, 'main')""",
            (performance_id, mbid),
        )
    return performance_id


def held_file(
    conn: Conn,
    work_id: str,
    performance_id: UUID | None,
    *,
    title: str = "Song",
    recording_id: str | None = None,
    enrichment: str = "enriched",
) -> UUID:
    file_id = uuid4()
    conn.execute(
        """INSERT INTO library_files (id, file_path, file_status, work_id, performance_id,
                                      recording_id, track_title, duration_ms,
                                      enrichment_status, format)
           VALUES (%s, %s, 'present', %s, %s, %s, %s, 200000, %s, 'flac')""",
        (
            file_id,
            f"D:/music/{file_id}.flac",
            work_id,
            performance_id,
            recording_id,
            title,
            enrichment,
        ),
    )
    return file_id


def recording(conn: Conn, work_id: str, title: str) -> str:
    recording_id = f"rec-{uuid4()}"
    conn.execute(
        """INSERT INTO recordings (id, work_id, title, version_type)
           VALUES (%s, %s, %s, 'original')""",
        (recording_id, work_id, title),
    )
    return recording_id


def tagged_mp3(folder: Path, name: str, artist_name: str, title: str) -> Path:
    """An MP3 whose only tags are its artist and title."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    shutil.copyfile(AUDIO, path)
    tags = ID3()
    tags.add(TPE1(encoding=3, text=artist_name))
    tags.add(TIT2(encoding=3, text=title))
    tags.save(str(path))
    return path


def mbid_mp3(folder: Path, name: str, artist_name: str, title: str, artist_mbid: str) -> Path:
    """An MP3 tagged with its artist, title and one artist MBID."""
    path = tagged_mp3(folder, name, artist_name, title)
    tags = ID3(str(path))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=[artist_mbid]))
    tags.save(str(path))
    return path


def file_by_name(conn: Conn, name: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, work_id, performance_id FROM library_files WHERE file_path LIKE %s",
        (f"%{name}",),
    ).fetchone()
    assert row is not None, name
    return dict(row)


def credited_ids(conn: Conn, performance_id: object) -> list[str | None]:
    rows = conn.execute(
        """SELECT artist_id FROM performance_credits WHERE performance_id = %s
           ORDER BY position""",
        (performance_id,),
    ).fetchall()
    return [r["artist_id"] for r in rows]


def page_works(client: TestClient, artist_id: str) -> set[str]:
    response = client.get(f"{ARTISTS}/{artist_id}")
    assert response.status_code == 200, response.text
    return {w["id"] for w in response.json()["works"]}


def list_work_counts(client: TestClient) -> dict[str, int]:
    response = client.get(ARTISTS, params={"limit": 500})
    assert response.status_code == 200, response.text
    return {i["id"]: i["work_count"] for i in response.json()["items"]}


# --- R6: artist pages by credits ------------------------------------------------------------------


def test_a_work_crediting_two_acts_is_on_both_pages_and_in_both_counts(
    app_client: TestClient, conn: Conn
) -> None:
    bon_jovi, nettles = artist(conn, "Bon Jovi"), artist(conn, "Jennifer Nettles")
    duet = work(conn, "Who Says You Can't Go Home")
    held_file(conn, duet, performance(conn, duet, bon_jovi, nettles))
    solo = work(conn, "Always")
    held_file(conn, solo, performance(conn, solo, bon_jovi))
    conn.commit()

    assert page_works(app_client, bon_jovi) == {duet, solo}
    assert page_works(app_client, nettles) == {duet}
    counts = list_work_counts(app_client)
    assert (counts[bon_jovi], counts[nettles]) == (2, 1)


def test_a_work_whose_performances_credit_another_act_is_not_on_the_page(
    app_client: TestClient, conn: Conn
) -> None:
    """A cover: the work credits only the covering act's performance, so only that act's page
    lists it."""
    poison, cooper = artist(conn, "Poison"), artist(conn, "Alice Cooper")
    cover = work(conn, "Your Mama Don't Dance")
    held_file(conn, cover, performance(conn, cover, poison))
    conn.commit()

    assert page_works(app_client, poison) == {cover}
    assert page_works(app_client, cooper) == set()


def test_a_work_with_no_performance_is_on_no_page(app_client: TestClient, conn: Conn) -> None:
    """D42: no file, no performance, no credit."""
    poison = artist(conn, "Poison")
    kept = work(conn, "Talk Dirty To Me")
    held_file(conn, kept, performance(conn, kept, poison))
    fileless = work(conn, "Talk Dirty To Me (Demo)")
    conn.commit()

    assert page_works(app_client, poison) == {kept}
    assert fileless not in page_works(app_client, poison)
    assert list_work_counts(app_client)[poison] == 1


def test_a_merged_members_credited_works_are_on_its_primarys_page(
    app_client: TestClient, conn: Conn
) -> None:
    heart, ann = artist(conn, "Heart"), artist(conn, "Ann Wilson")
    conn.execute("INSERT INTO artist_merges (artist_id, primary_id) VALUES (%s, %s)", (ann, heart))
    barracuda = work(conn, "Barracuda")
    held_file(conn, barracuda, performance(conn, barracuda, heart))
    alone = work(conn, "Alone")
    held_file(conn, alone, performance(conn, alone, ann))
    conn.commit()

    assert page_works(app_client, heart) == {barracuda, alone}
    assert list_work_counts(app_client)[heart] == 2


def test_a_work_whose_only_credit_is_an_unheld_mbid_is_on_no_page(
    app_client: TestClient, conn: Conn
) -> None:
    """SPEC D52 (Lance, 2026-11-21): the pages stay the credits alone. A work whose only credit
    is an MBID no artist holds is on no artist page and in no count, even beside a name-only
    artist of the name its files carry; it shows once an artist holds the MBID."""
    shared = artist(conn, "Shared Name")
    untagged = work(conn, "Morning Light")
    held_file(conn, untagged, performance(conn, untagged, shared), title="Morning Light")
    tagged = work(conn, "Evening Rain")
    held_file(conn, tagged, performance(conn, tagged, mbid=UNHELD_MBID), title="Evening Rain")
    conn.commit()

    assert page_works(app_client, shared) == {untagged}
    assert sum(list_work_counts(app_client).values()) == 1

    holder = artist(conn, "Shared Name (MusicBrainz)", mbid=UNHELD_MBID)
    conn.commit()
    assert page_works(app_client, holder) == {tagged}


# --- R1: grouping through credits -----------------------------------------------------------------


def test_a_scanned_file_joins_the_work_its_artist_is_credited_on(
    conn: Conn, tmp_path: Path
) -> None:
    """The work's only tie to Poison is its performance's credit; the new file joins it, and its
    performance credits the artist grouping picked."""
    poison = artist(conn, "Poison")
    talk = work(conn, "Talk Dirty To Me")
    held_file(conn, talk, performance(conn, talk, poison), title="Talk Dirty To Me")
    conn.commit()
    library = tmp_path / "library"
    tagged_mp3(library, "talk.mp3", "Poison", "Talk Dirty To Me")

    full_scan(conn, library)

    scanned = file_by_name(conn, "talk.mp3")
    assert scanned["work_id"] == talk
    assert credited_ids(conn, scanned["performance_id"]) == [poison]


def test_a_second_credited_act_counts_for_grouping(conn: Conn, tmp_path: Path) -> None:
    bon_jovi, nettles = artist(conn, "Bon Jovi"), artist(conn, "Jennifer Nettles")
    duet = work(conn, "Who Says You Can't Go Home")
    held_file(
        conn, duet, performance(conn, duet, bon_jovi, nettles), title="Who Says You Can't Go Home"
    )
    conn.commit()
    library = tmp_path / "library"
    tagged_mp3(library, "duet.mp3", "Jennifer Nettles", "Who Says You Can't Go Home")

    full_scan(conn, library)

    assert file_by_name(conn, "duet.mp3")["work_id"] == duet


def test_a_tie_goes_to_the_musicbrainz_work(conn: Conn, tmp_path: Path) -> None:
    """#258's order (AUD-R103): two works of the same title credit Poison; the MusicBrainz one
    takes the file, whatever their ids."""
    poison = artist(conn, "Poison")
    local = work(conn, "Every Rose Has Its Thorn")
    held_file(conn, local, performance(conn, local, poison), title="Every Rose Has Its Thorn")
    musicbrainz = work(
        conn, "Every Rose Has Its Thorn", mbid="0c000000-0000-4000-8000-0000000000e3"
    )
    held_file(
        conn, musicbrainz, performance(conn, musicbrainz, poison), title="Every Rose Has Its Thorn"
    )
    conn.commit()
    library = tmp_path / "library"
    tagged_mp3(library, "rose.mp3", "Poison", "Every Rose Has Its Thorn")

    full_scan(conn, library)

    assert file_by_name(conn, "rose.mp3")["work_id"] == musicbrainz


def test_a_file_whose_artist_is_credited_nowhere_gets_a_new_work_crediting_it(
    conn: Conn, tmp_path: Path
) -> None:
    library = tmp_path / "library"
    tagged_mp3(library, "cherry.mp3", "Warrant", "Cherry Pie")

    full_scan(conn, library)

    scanned = file_by_name(conn, "cherry.mp3")
    assert scanned["work_id"] is not None and scanned["performance_id"] is not None
    picked = conn.execute(
        "SELECT id, name FROM artists WHERE normalized_name = 'warrant'"
    ).fetchone()
    assert picked is not None and picked["name"] == "Warrant"
    assert credited_ids(conn, scanned["performance_id"]) == [picked["id"]]


def held_mbids(conn: Conn) -> list[str]:
    rows = conn.execute("SELECT mbid FROM artists WHERE mbid IS NOT NULL").fetchall()
    return [r["mbid"] for r in rows]


def test_tagged_copies_of_an_mbid_no_artist_holds_share_a_work(conn: Conn, tmp_path: Path) -> None:
    """SPEC D51 (Lance, 2026-11-21): a name with one MBID makes no artist for it (2a D6), so a
    tagged file's performance credits the MBID alone, held by no artist. Grouping still finds
    that work by the name its files carry: two tagged copies of one song make one work, not two.
    """
    library = tmp_path / "library"
    mbid_mp3(library, "round-1.mp3", "Ratt", "Round And Round", RATT_MBID)
    mbid_mp3(library, "round-2.mp3", "Ratt", "Round And Round", RATT_MBID)

    full_scan(conn, library)

    first, second = file_by_name(conn, "round-1.mp3"), file_by_name(conn, "round-2.mp3")
    assert RATT_MBID not in held_mbids(conn)
    assert first["work_id"] is not None
    assert second["work_id"] == first["work_id"]


def test_a_later_tagged_copy_of_an_mbid_no_artist_holds_joins_its_work(
    conn: Conn, tmp_path: Path
) -> None:
    """SPEC D51, across scans: the copy a later scan finds joins the first copy's work."""
    library = tmp_path / "library"
    mbid_mp3(library, "round-1.mp3", "Ratt", "Round And Round", RATT_MBID)
    full_scan(conn, library)
    first = file_by_name(conn, "round-1.mp3")

    mbid_mp3(library, "round-2.mp3", "Ratt", "Round And Round", RATT_MBID)
    full_scan(conn, library)

    assert RATT_MBID not in held_mbids(conn)
    assert file_by_name(conn, "round-2.mp3")["work_id"] == first["work_id"]


def test_a_moved_file_that_held_no_performance_takes_its_new_works_default_act(
    conn: Conn,
) -> None:
    """Lock request reading: neither grouping nor the build names an artist for a file a writer
    moves before it ever held a performance (D18 keeps a held one's act), so its new performance
    credits its new work's default act: the credit of the performance holding the master's
    file."""
    poison = artist(conn, "Poison")
    home = work(conn, "Talk Dirty To Me")
    master_file = held_file(conn, home, performance(conn, home, poison))
    conn.execute(
        "INSERT INTO song_masters (work_id, preferred_file_id) VALUES (%s, %s)",
        (home, master_file),
    )
    unplaced = held_file(conn, work(conn, "Talk Dirty"), None, title="Talk Dirty To Me")

    reassign_file(unplaced, home, work_curation_repos(RepositoryFactory(conn)))

    row = conn.execute(
        "SELECT performance_id FROM library_files WHERE id = %s", (unplaced,)
    ).fetchone()
    assert row is not None and row["performance_id"] is not None
    assert credited_ids(conn, row["performance_id"]) == [poison]


# --- R4: the untagged pass ------------------------------------------------------------------------


def test_the_untagged_pass_searches_with_the_files_own_credits_mbid(conn: Conn) -> None:
    """By artist id (the artist's MBID) or by MBID alone; a name-only credit, or no
    performance, gives the pass nothing to search with."""
    bon_jovi = artist(conn, "Bon Jovi", BON_JOVI_MBID)
    poison = artist(conn, "Poison")
    song = work(conn, "Always")
    by_id = held_file(conn, song, performance(conn, song, bon_jovi), enrichment="pending")
    by_mbid = held_file(
        conn, song, performance(conn, song, mbid=NETTLES_MBID), enrichment="pending"
    )
    held_file(conn, song, performance(conn, song, poison), enrichment="pending")
    held_file(conn, song, None, enrichment="pending")

    found = PgLibraryFileRepository(conn).list_untagged_with_linked_artist()

    assert {(c.file.id, c.artist_mbid) for c in found} == {
        (by_id, BON_JOVI_MBID),
        (by_mbid, NETTLES_MBID),
    }


# --- R7: Unmapped versions ------------------------------------------------------------------------


def unmapped(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/api/v1/library/versions", params={"limit": 50})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def played_version(conn: Conn, work_id: str, performance_id: UUID | None, title: str) -> None:
    """A line titled *title* (a version text) matched to a new file of the work, played once."""
    file_id = held_file(conn, work_id, performance_id, title=title.split(" (")[0])
    line = broadcast.line(conn, title, file_id)
    broadcast.plays(
        conn, line, broadcast.station(conn, "KISW"), datetime(2001, 1, 1, 8, tzinfo=UTC)
    )


def test_an_unmapped_version_names_its_works_credited_artists(
    app_client: TestClient, conn: Conn
) -> None:
    bon_jovi, nettles = artist(conn, "Bon Jovi"), artist(conn, "Jennifer Nettles")
    duet = work(conn, "Who Says You Can't Go Home")
    played_version(
        conn, duet, performance(conn, duet, bon_jovi, nettles), "Who Says You Can't Go Home (Live)"
    )
    conn.commit()

    [item] = unmapped(app_client)

    assert item["work_id"] == duet
    assert item["artists"] == [
        {"id": bon_jovi, "name": "Bon Jovi"},
        {"id": nettles, "name": "Jennifer Nettles"},
    ]
    assert "artist_id" not in item and "artist_name" not in item


def test_an_artist_pages_dismiss_all_takes_the_works_its_group_is_credited_on(
    app_client: TestClient, conn: Conn
) -> None:
    """Heart's group (Ann Wilson merged into it): Barracuda credits Heart, Alone credits Ann as
    its second act. Another act's work keeps its text."""
    heart, ann, poison = artist(conn, "Heart"), artist(conn, "Ann Wilson"), artist(conn, "Poison")
    conn.execute("INSERT INTO artist_merges (artist_id, primary_id) VALUES (%s, %s)", (ann, heart))
    barracuda = work(conn, "Barracuda")
    played_version(conn, barracuda, performance(conn, barracuda, heart), "Barracuda (Live)")
    alone = work(conn, "Alone")
    played_version(conn, alone, performance(conn, alone, poison, ann), "Alone (Live)")
    talk = work(conn, "Talk Dirty To Me")
    played_version(conn, talk, performance(conn, talk, poison), "Talk Dirty To Me (Live)")
    conn.commit()

    response = app_client.post("/api/v1/library/versions/dismiss", json={"artist_id": heart})

    assert response.status_code == 200, response.text
    assert response.json() == {"dismissed": 2}
    assert [i["work_id"] for i in unmapped(app_client)] == [talk]


# --- the work page --------------------------------------------------------------------------------


def test_the_work_detail_names_no_artist_of_its_own(app_client: TestClient, conn: Conn) -> None:
    """``artist_id`` is gone from the detail; a copy shown through a shared recording names its
    work by credits only."""
    poison, labelle = artist(conn, "Poison"), artist(conn, "Labelle")
    talk = work(conn, "Talk Dirty To Me")
    shared = recording(conn, talk, "Talk Dirty To Me")
    held_file(conn, talk, performance(conn, talk, poison), recording_id=shared)
    elsewhere = work(conn, "Lady Marmalade")
    held_file(conn, elsewhere, performance(conn, elsewhere, labelle), recording_id=shared)
    conn.commit()

    response = app_client.get(f"/api/v1/library/works/{talk}")

    assert response.status_code == 200, response.text
    detail = response.json()
    assert "artist_id" not in detail
    assert detail["artists"] == [{"id": poison, "name": "Poison"}]
    [other] = [f["other_work"] for r in detail["recordings"] for f in r["files"] if f["other_work"]]
    assert other == {"id": elsewhere, "title": "Lady Marmalade", "artists": ["Labelle"]}
