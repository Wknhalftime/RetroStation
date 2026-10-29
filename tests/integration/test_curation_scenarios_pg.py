"""Characterisation, artist model phase 0a (SPEC S1, E3, E4): what plays around each curation
write.

Each test replays one curation write through its route or task entry point and asserts what
plays for named log lines at named stations, before and after. What plays is read only through
the curation view ``play_file_resolution``: the file a play resolves to, or None when nothing
plays.

Seeds follow the SPEC's Context rule, so phases 3a and 3b run these tests unchanged. The library
comes from the scan (a task entry point) over tagged copies of a fixture file; stations, log
lines, matches and plays come from the locked ``stream_seed``; every write goes through a route
or a task entry point. Nothing here writes ``matches.artist_id``, ``works.artist_id`` or
``matches.work_id``, or calls ``Match(artist_id=...)``, ``create_local`` or ``upsert_from_mb``.

Master scores (``master_selection_service``): MP3 3, plus album 80, single 60, compilation 40,
live 30, plus 100 for a promotional release. The scan never re-picks the master of a work a new
file joins; the first file of a new work is its master.

Reject and Unmatch of a log name are in test_curation_cascades_pg.py (1b relocks it, AUD-R037);
the work merge is in test_curation_merges_pg.py (3b relocks it, AUD-R032).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path, PureWindowsPath
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from mutagen.id3 import ID3, TIT2, TPE1, TXXX
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.tasks import library_enrichment_tasks
from tests.fakes.mb_client import FakeMbClient
from tests.integration import stream_seed as seed
from tests.integration.missing_file_seed import full_scan, put, retag
from tests.integration.stream_seed import at
from tests.lifespan_client import lifespan_client

type Conn = psycopg.Connection[DictRow]

# psycopg's async pool needs the selector event loop on Windows (as tests/routers/conftest.py).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# --- the app, the database and the tasks' settings --------------------------------------------


@pytest.fixture(scope="module")
def app_client(_migrated_db_url: str) -> Iterator[TestClient]:
    """The app on the test database, its lifespan run once per module; only the token is bypassed.

    DATABASE_URL names the test database for the whole module, so the sync routes, the
    post-commit master recalculation and the task entry points all use it.
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


# --- seeds: tagged files, lines and plays --------------------------------------------------------


def tagged(
    dest: Path,
    artist: str,
    title: str,
    *,
    release_type: str = "album",
    promotion: bool = False,
    recording_mbid: str | None = None,
    release_mbid: str | None = None,
) -> Path:
    """A copy of the fixture MP3 at *dest* carrying only these tags (the scan reads them)."""
    put("well_tagged.mp3", dest)
    tags = ID3(str(dest))
    tags.clear()
    tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Release Type", text=[release_type]))
    if promotion:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Release Status", text=["promotion"]))
    if recording_mbid is not None:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=[recording_mbid]))
    if release_mbid is not None:
        tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=[release_mbid]))
    tags.save()
    return dest


def file_named(conn: Conn, name: str) -> UUID:
    """The one library row whose file is called *name* (compared by name, never by LIKE)."""
    rows = conn.execute("SELECT id, file_path FROM library_files").fetchall()
    [file_id] = [UUID(str(r["id"])) for r in rows if PureWindowsPath(r["file_path"]).name == name]
    return file_id


def work_id_of(conn: Conn, file_id: UUID) -> str:
    """The id of the work a file is on, to address the works routes; never asserted on."""
    row = conn.execute("SELECT work_id FROM library_files WHERE id = %s", (file_id,)).fetchone()
    assert row is not None and row["work_id"] is not None
    return str(row["work_id"])


def matched_line(conn: Conn, file_id: UUID, *, artist: str, title: str) -> UUID:
    """A log line, auto-matched to *file_id*; returns the line (track identity) id."""
    identity = seed.identity(conn, title=title, artist=artist)
    seed.match(conn, identity, file_id)
    return identity


def play_at(conn: Conn, station: UUID, line: UUID, hms: str = "08:00") -> UUID:
    """One play of *line* at *station*, at station wall-clock *hms*; returns the play id."""
    return seed.play(conn, seed.playlist(conn, station), line, at(hms))


def plays(conn: Conn, event: UUID) -> UUID | None:
    """The file *event* resolves to, read only through the view; None when nothing plays."""
    [row] = conn.execute(
        "SELECT file_id FROM play_file_resolution WHERE play_event_id = %s", (event,)
    ).fetchall()
    return None if row["file_id"] is None else UUID(str(row["file_id"]))


def resolution_count(conn: Conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM play_file_resolution").fetchone()
    assert row is not None
    return int(row["n"])


# --- S1: one test per curation write -------------------------------------------------------------


def test_new_file_arrives(conn: Conn, tmp_path: Path) -> None:
    """S1, a new file arrives (the scan).

    A second copy of KISS joins its work and the scan keeps the work's master, though the new
    copy scores higher, so a line matched to the new copy plays the old one; a new song gets a
    work of its own, with itself as master. A line still waiting for a match plays nothing,
    before and after: a new file does not match a line by itself.
    """
    root = tmp_path / "library"
    tagged(root / "kiss-single.mp3", "Prince", "Kiss", release_type="single")
    full_scan(conn, root)
    single = file_named(conn, "kiss-single.mp3")
    kaaa = seed.station(conn, "AC")
    kiss = matched_line(conn, single, artist="PRINCE", title="KISS")
    purple_rain = seed.identity(conn, artist="PRINCE", title="PURPLE RAIN", status="pending")
    conn.commit()
    assert resolution_count(conn) == 0  # no plays yet: nothing resolves

    kiss_at_kaaa = play_at(conn, kaaa, kiss)
    purple_rain_at_kaaa = play_at(conn, kaaa, purple_rain, "08:04")
    conn.commit()
    assert (plays(conn, kiss_at_kaaa), plays(conn, purple_rain_at_kaaa)) == (single, None)

    tagged(root / "kiss-album.mp3", "Prince", "Kiss", release_type="album")
    tagged(root / "purple-rain.mp3", "Prince", "Purple Rain")
    full_scan(conn, root)

    album, new_song = file_named(conn, "kiss-album.mp3"), file_named(conn, "purple-rain.mp3")
    kbbb = seed.station(conn, "CHR")
    album_at_kbbb = play_at(conn, kbbb, matched_line(conn, album, artist="PRINCE", title="KISS"))
    new_song_at_kbbb = play_at(
        conn, kbbb, matched_line(conn, new_song, artist="PRINCE", title="PURPLE RAIN"), "08:04"
    )
    conn.commit()
    assert (plays(conn, kiss_at_kaaa), plays(conn, purple_rain_at_kaaa)) == (single, None)
    assert (plays(conn, album_at_kbbb), plays(conn, new_song_at_kbbb)) == (single, new_song)


def test_missing_file_folds_into_its_replacement(conn: Conn, tmp_path: Path) -> None:
    """S1, a missing file folds into its replacement (the scan's reconciliation).

    LOSER's file is moved and retagged in one go (as Picard does): the scan marks the old row
    missing, indexes the new path as a new row and folds the old row into it, so the line
    plays the replacement.
    """
    root = tmp_path / "library"
    same_recording = {
        "recording_mbid": "0a000000-0000-4000-8000-000000000001",
        "release_mbid": "0a000000-0000-4000-8000-000000000002",
    }
    old = tagged(root / "old" / "loser.mp3", "3 Doors Down", "Loser", **same_recording)
    full_scan(conn, root)
    gone = file_named(conn, "loser.mp3")
    kaaa = seed.station(conn, "AC")
    loser_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, gone, artist="3 DOORS DOWN", title="LOSER")
    )
    conn.commit()
    assert plays(conn, loser_at_kaaa) == gone

    old.unlink()
    retag(tagged(root / "new" / "loser-retagged.mp3", "3 Doors Down", "Loser", **same_recording))
    full_scan(conn, root)

    assert plays(conn, loser_at_kaaa) == file_named(conn, "loser-retagged.mp3")


_RELEASE = "0a000000-0000-4000-8000-000000000e01"
_RECORDING = "0a000000-0000-4000-8000-000000000e02"
_MB_ARTIST = "0a000000-0000-4000-8000-000000000e03"
_MB_WORK = "0a000000-0000-4000-8000-000000000e04"
_CREDIT = [{"artist": {"id": _MB_ARTIST, "name": "Test Artist", "sort-name": "Artist, Test"}}]
_RECORDING_DATA = {
    "id": _RECORDING,
    "title": "Test Track",
    "length": 240_000,
    "artist-credit": _CREDIT,
    "relations": [{"type": "performance", "work": {"id": _MB_WORK, "title": "Test Work"}}],
}
_RELEASE_DATA = {
    "id": _RELEASE,
    "title": "Test Album",
    "artist-credit": _CREDIT,
    "media": [{"tracks": [{"recording": _RECORDING_DATA}]}],
}


@pytest.fixture
def tasks_on_test_db(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The task entry points read their database from the settings: point them here."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_enrichment_moves_a_file_to_another_work(
    conn: Conn, tmp_path: Path, tasks_on_test_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S1, enrichment moves a file to another work (the library enrichment task).

    MusicBrainz (a fake) says the tagged copy of TEST TRACK performs another work, so
    enrichment moves it there; the work it left re-picks its master from the copy it keeps.
    Each line then plays its own copy.
    """
    root = tmp_path / "library"
    tagged(
        root / "track-tagged.mp3",
        "Test Artist",
        "Test Track",
        recording_mbid=_RECORDING,
        release_mbid=_RELEASE,
    )
    full_scan(conn, root)
    tagged(root / "track-untagged.mp3", "Test Artist", "Test Track", release_type="single")
    full_scan(conn, root)
    moving, staying = file_named(conn, "track-tagged.mp3"), file_named(conn, "track-untagged.mp3")
    kaaa, kbbb = seed.station(conn, "AC"), seed.station(conn, "CHR")
    moving_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, moving, artist="TEST ARTIST", title="TEST TRACK")
    )
    staying_at_kbbb = play_at(
        conn, kbbb, matched_line(conn, staying, artist="TEST ARTIST", title="TEST TRACK")
    )
    conn.commit()
    assert (plays(conn, moving_at_kaaa), plays(conn, staying_at_kbbb)) == (moving, moving)

    fake = FakeMbClient(
        releases={_RELEASE: _RELEASE_DATA}, recordings={_RECORDING: _RECORDING_DATA}
    )
    monkeypatch.setattr(
        library_enrichment_tasks, "MusicBrainzApiClient", lambda *_a, **_k: nullcontext(fake)
    )
    library_enrichment_tasks.library_enrichment_task.call_local()

    assert (plays(conn, moving_at_kaaa), plays(conn, staying_at_kbbb)) == (moving, staying)


def test_set_master_picks_a_file_of_another_recording(
    conn: Conn, client: TestClient, tmp_path: Path
) -> None:
    """S1, Set master picks a file of another recording (PUT /works/{id}/master).

    NOTHING ELSE MATTERS holds the studio and the live recording; every line of the work,
    whichever copy it matched, plays the master the curator picks.
    """
    root = tmp_path / "library"
    tagged(root / "nem-studio.mp3", "Metallica", "Nothing Else Matters")
    full_scan(conn, root)
    tagged(root / "nem-live.mp3", "Metallica", "Nothing Else Matters (Live)", release_type="live")
    full_scan(conn, root)
    studio, live = file_named(conn, "nem-studio.mp3"), file_named(conn, "nem-live.mp3")
    work = work_id_of(conn, studio)
    kaaa, kbbb = seed.station(conn, "AC"), seed.station(conn, "CHR")
    studio_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, studio, artist="METALLICA", title="NOTHING ELSE MATTERS")
    )
    live_at_kbbb = play_at(
        conn, kbbb, matched_line(conn, live, artist="METALLICA", title="NOTHING ELSE MATTERS LIVE")
    )
    conn.commit()
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kbbb)) == (studio, studio)

    response = client.put(
        f"/api/v1/library/works/{work}/master", json={"preferred_file_id": str(live)}
    )

    assert response.status_code == 200
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kbbb)) == (live, live)


def test_split_takes_a_file_to_a_work_of_its_own(
    conn: Conn, client: TestClient, tmp_path: Path
) -> None:
    """S1, a work split (POST /works/{id}/split): the split-off copy's line plays that copy."""
    root = tmp_path / "library"
    tagged(root / "lithium.mp3", "Nirvana", "Lithium")
    full_scan(conn, root)
    tagged(root / "lithium-live.mp3", "Nirvana", "Lithium (Live)", release_type="live")
    full_scan(conn, root)
    studio, live = file_named(conn, "lithium.mp3"), file_named(conn, "lithium-live.mp3")
    work = work_id_of(conn, studio)
    kaaa = seed.station(conn, "AC")
    studio_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, studio, artist="NIRVANA", title="LITHIUM")
    )
    live_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, live, artist="NIRVANA", title="LITHIUM (LIVE)"), "08:04"
    )
    conn.commit()
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kaaa)) == (studio, studio)

    response = client.post(f"/api/v1/library/works/{work}/split", json={"file_id": str(live)})

    assert response.status_code == 201
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kaaa)) == (studio, live)


def test_reassign_moves_a_file_to_another_work(
    conn: Conn, client: TestClient, tmp_path: Path
) -> None:
    """S1, a reassign (PATCH /files/{id}/work).

    A promotional copy of COMEDOWN was tagged GLYCERINE, so the scan put it on GLYCERINE's
    work. Reassigned to COMEDOWN's work, it becomes that work's automatic master (it scores
    higher), so COMEDOWN's own line moves to it too; GLYCERINE's line keeps its master.
    """
    root = tmp_path / "library"
    tagged(root / "comedown.mp3", "Bush", "Comedown", release_type="single")
    tagged(root / "glycerine.mp3", "Bush", "Glycerine")
    full_scan(conn, root)
    tagged(root / "comedown-promo.mp3", "Bush", "Glycerine", promotion=True)
    full_scan(conn, root)
    comedown, glycerine = file_named(conn, "comedown.mp3"), file_named(conn, "glycerine.mp3")
    misfiled = file_named(conn, "comedown-promo.mp3")
    kaaa, kbbb, kccc = seed.station(conn, "AC"), seed.station(conn, "CHR"), seed.station(conn)
    comedown_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, comedown, artist="BUSH", title="COMEDOWN")
    )
    misfiled_at_kbbb = play_at(
        conn, kbbb, matched_line(conn, misfiled, artist="BUSH", title="COMEDOWN")
    )
    glycerine_at_kccc = play_at(
        conn, kccc, matched_line(conn, glycerine, artist="BUSH", title="GLYCERINE")
    )
    conn.commit()
    before = [plays(conn, e) for e in (comedown_at_kaaa, misfiled_at_kbbb, glycerine_at_kccc)]
    assert before == [comedown, glycerine, glycerine]

    response = client.patch(
        f"/api/v1/library/files/{misfiled}/work", json={"work_id": work_id_of(conn, comedown)}
    )

    assert response.status_code == 200
    after = [plays(conn, e) for e in (comedown_at_kaaa, misfiled_at_kbbb, glycerine_at_kccc)]
    assert after == [misfiled, misfiled, glycerine]


def _catalog_artist(client: TestClient, name: str) -> str:
    """The id the Library's artist list gives the catalog artist called *name*."""
    response = client.get("/api/v1/library/artists", params={"search": name})
    assert response.status_code == 200
    [artist_id] = [a["id"] for a in response.json()["items"] if a["name"] == name]
    return str(artist_id)


def _log_name_of(conn: Conn, line: UUID) -> UUID:
    row = conn.execute(
        "SELECT broadcast_artist_id FROM track_identities WHERE id = %s", (line,)
    ).fetchone()
    assert row is not None
    return UUID(str(row["broadcast_artist_id"]))


def test_manual_resolve_artist_then_song(conn: Conn, client: TestClient, tmp_path: Path) -> None:
    """S1, manual Resolve of a log name's artist, then of its song (the matching routes).

    Resolving the artist only sends the song back to matching, so it still plays nothing.
    Resolving the song plays its work, and the resolve re-picks that work's automatic master:
    the album copy scores higher than the single both lines matched, so both move to it.
    """
    root = tmp_path / "library"
    tagged(root / "linger-single.mp3", "The Cranberries", "Linger", release_type="single")
    full_scan(conn, root)
    tagged(root / "linger-album.mp3", "The Cranberries", "Linger")
    full_scan(conn, root)
    single, album = file_named(conn, "linger-single.mp3"), file_named(conn, "linger-album.mp3")
    kaaa, kbbb = seed.station(conn, "AC"), seed.station(conn, "CHR")
    matched_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, single, artist="THE CRANBERRIES", title="LINGER")
    )
    waiting = seed.identity(conn, artist="CRANBERRIES", title="LINGER", status="needs_review")
    waiting_at_kbbb = play_at(conn, kbbb, waiting)
    conn.commit()
    assert (plays(conn, matched_at_kaaa), plays(conn, waiting_at_kbbb)) == (single, None)

    artist = client.post(
        f"/api/v1/matching/artists/{_log_name_of(conn, waiting)}/resolve",
        json={
            "match_status": "manual_matched",
            "target_artist_id": _catalog_artist(client, "The Cranberries"),
        },
    )

    assert artist.status_code == 200
    assert (plays(conn, matched_at_kaaa), plays(conn, waiting_at_kbbb)) == (single, None)

    song = client.post(
        f"/api/v1/matching/identities/{waiting}/resolve",
        json={"match_status": "manual_matched", "library_file_id": str(single)},
    )

    assert song.status_code == 200
    assert (plays(conn, matched_at_kaaa), plays(conn, waiting_at_kbbb)) == (album, album)


def _two_copy_work(conn: Conn, root: Path) -> tuple[str, UUID, UUID]:
    """LET HER CRY: an album copy (the master) and an AC edit on one work."""
    tagged(root / "let-her-cry.mp3", "Hootie & the Blowfish", "Let Her Cry")
    full_scan(conn, root)
    tagged(
        root / "let-her-cry-ac.mp3",
        "Hootie & the Blowfish",
        "Let Her Cry (AC Edit)",
        release_type="single",
    )
    full_scan(conn, root)
    album, ac_edit = file_named(conn, "let-her-cry.mp3"), file_named(conn, "let-her-cry-ac.mp3")
    work = work_id_of(conn, album)
    return work, album, ac_edit


def test_format_override_set_and_removed(conn: Conn, client: TestClient, tmp_path: Path) -> None:
    """S1, a format override is set, then removed (the works routes).

    One line of LET HER CRY plays at an AC and a CHR station. The AC override plays at the AC
    station only; removed, both stations play the master again.
    """
    work, album, ac_edit = _two_copy_work(conn, tmp_path / "library")
    line = matched_line(conn, album, artist="HOOTIE & THE BLOWFISH", title="LET HER CRY")
    at_kaaa = play_at(conn, seed.station(conn, "AC"), line)
    at_kbbb = play_at(conn, seed.station(conn, "CHR"), line)
    conn.commit()
    assert (plays(conn, at_kaaa), plays(conn, at_kbbb)) == (album, album)

    created = client.post(
        f"/api/v1/library/works/{work}/format-overrides",
        json={"format_name": "AC", "preferred_file_id": str(ac_edit)},
    )

    assert created.status_code == 201
    assert (plays(conn, at_kaaa), plays(conn, at_kbbb)) == (ac_edit, album)

    removed = client.delete(f"/api/v1/library/works/{work}/format-overrides/{created.json()['id']}")

    assert removed.status_code == 204
    assert (plays(conn, at_kaaa), plays(conn, at_kbbb)) == (album, album)


# --- E3, E4 ----------------------------------------------------------------------------------


def test_formatless_station(conn: Conn, client: TestClient, tmp_path: Path) -> None:
    """E3: a station with no format plays the master, never an override.

    The work has overrides for AC and CHR; the same line plays the AC edit at the AC station
    and the master at the station with no format.
    """
    work, album, ac_edit = _two_copy_work(conn, tmp_path / "library")
    for format_name in ("AC", "CHR"):
        response = client.post(
            f"/api/v1/library/works/{work}/format-overrides",
            json={"format_name": format_name, "preferred_file_id": str(ac_edit)},
        )
        assert response.status_code == 201
    line = matched_line(conn, album, artist="HOOTIE & THE BLOWFISH", title="LET HER CRY")
    at_formatless = play_at(conn, seed.station(conn, None), line)
    at_kaaa = play_at(conn, seed.station(conn, "AC"), line)
    conn.commit()

    assert (plays(conn, at_formatless), plays(conn, at_kaaa)) == (album, ac_edit)


@contextmanager
def master_writes_refused(conn: Conn) -> Iterator[None]:
    """Every song-master write fails in the database until the block ends."""
    conn.execute(
        """CREATE FUNCTION phase0a_refuse_master_write() RETURNS trigger
           LANGUAGE plpgsql AS $$
           BEGIN
               RAISE EXCEPTION 'phase 0a: song master write refused';
           END $$"""
    )
    conn.execute(
        """CREATE TRIGGER phase0a_refuse_master_write
           BEFORE INSERT OR UPDATE ON song_masters
           FOR EACH ROW EXECUTE FUNCTION phase0a_refuse_master_write()"""
    )
    conn.commit()
    try:
        yield
    finally:
        conn.rollback()
        conn.execute("DROP TRIGGER phase0a_refuse_master_write ON song_masters")
        conn.execute("DROP FUNCTION phase0a_refuse_master_write()")
        conn.commit()


def test_failed_write_changes_nothing(conn: Conn, client: TestClient, tmp_path: Path) -> None:
    """E4: a split that fails halfway (at its song-master write, after the file has moved to
    the new work) rolls back, and what plays is unchanged."""
    root = tmp_path / "library"
    tagged(root / "lithium.mp3", "Nirvana", "Lithium")
    full_scan(conn, root)
    tagged(root / "lithium-live.mp3", "Nirvana", "Lithium (Live)", release_type="live")
    full_scan(conn, root)
    studio, live = file_named(conn, "lithium.mp3"), file_named(conn, "lithium-live.mp3")
    work = work_id_of(conn, studio)
    kaaa = seed.station(conn, "AC")
    studio_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, studio, artist="NIRVANA", title="LITHIUM")
    )
    live_at_kaaa = play_at(
        conn, kaaa, matched_line(conn, live, artist="NIRVANA", title="LITHIUM (LIVE)"), "08:04"
    )
    conn.commit()
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kaaa)) == (studio, studio)

    with master_writes_refused(conn):
        response = client.post(f"/api/v1/library/works/{work}/split", json={"file_id": str(live)})

    assert not response.is_success
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kaaa)) == (studio, studio)

    # The refusal alone failed it: with the master write allowed, the same split of the same
    # file (still on the work, or the route would refuse it) goes through.
    retried = client.post(f"/api/v1/library/works/{work}/split", json={"file_id": str(live)})

    assert retried.status_code == 201
    assert (plays(conn, studio_at_kaaa), plays(conn, live_at_kaaa)) == (studio, live)
