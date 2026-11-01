"""Naming the stub artists (artist model phase 1a, PR 2; SPEC S4, E1-E3).

A stub (S3, S6, S9) is an artist whose name is still its MBID, with no normalized name; S15
hides it. MusicBrainz names it: ``_enhance_artist`` checks a stub's MBID with
``MusicBrainzId.parse``, then takes the name, sort name and disambiguation from
``lookup_artist`` and the normalized name from the name. An artist that is no stub keeps its
name. ``scripts/name_stubs.py`` names every artist whose name still equals its MBID, whether or
not enrichment has marked it enhanced, commits each, and queues nothing; enrichment names the
later stubs as it runs.

E1: when another artist already holds the name's normalized form, found by
``get_by_normalized_name`` before the write, the stub keeps its normalized name NULL and a
warning names it. E2: a stub MusicBrainz did not answer for stays a stub, the run returns, and
the next run names it. E3: a 404 or a malformed MBID is recorded in ``enhancement_error`` once
and never looked up again. E1 and E3 run through both the script and enrichment.

MusicBrainz is ``FakeMbClient``; the catalog is PostgreSQL, read back on a second connection,
so only what was committed counts.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from structlog.testing import capture_logs

from backend.db.repositories.artists import PgArtistRepository
from backend.domain.catalog import Artist
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.tasks import cue_huey_app, huey_app
from backend.tasks.mb_enrichment_tasks import ArtistEnhanceOutcome, _enhance_artist
from tests.fakes.mb_client import FakeMbClient

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]
type Namer = Callable[[Conn, FakeMbClient], None]

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

BOC = "a0000000-0000-4000-8000-000000000001"
RUSH = "a0000000-0000-4000-8000-000000000002"
KISS = "a0000000-0000-4000-8000-000000000003"
METALLICA = "a0000000-0000-4000-8000-000000000004"
NIRVANA_US = "a0000000-0000-4000-8000-000000000005"
NIRVANA_UK = "a0000000-0000-4000-8000-000000000006"
GONE = "a0000000-0000-4000-8000-000000000007"
MALFORMED = "artist-mbid-0001"  # what a Resolve before S4 could store (SPEC Context)


def _mb(mbid: str, name: str, sort_name: str, disambiguation: str = "") -> dict[str, Any]:
    return {"id": mbid, "name": name, "sort-name": sort_name, "disambiguation": disambiguation}


MB = {
    BOC: _mb(BOC, "Blue Öyster Cult", "Blue Öyster Cult", "US hard rock band"),
    RUSH: _mb(RUSH, "Rush", "Rush", "Canadian rock band"),
    KISS: _mb(KISS, "KISS", "KISS", "US rock band"),
    METALLICA: _mb(METALLICA, "Metallica", "Metallica"),
    NIRVANA_US: _mb(NIRVANA_US, "Nirvana", "Nirvana", "90s US grunge band"),
    NIRVANA_UK: _mb(NIRVANA_UK, "Nirvana", "Nirvana", "60s UK band"),
}


@pytest.fixture(scope="module")
def script() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    return importlib.import_module("name_stubs")


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    """The connection the naming runs on."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def reader(migrated_db: str) -> Iterator[Conn]:
    """A second connection, which sees only what the naming committed."""
    with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as connection:
        yield connection


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Whatever reaches either Huey queue."""
    seen: list[Any] = []
    for queue in (huey_app.huey, cue_huey_app.cue_huey):
        monkeypatch.setattr(queue, "enqueue", seen.append)
    return seen


def _enrich(conn: Conn, client: FakeMbClient) -> None:
    """Enrichment's artist loop: ``_enhance_artist`` on each artist it queues, committed."""
    for artist in PgArtistRepository(conn).list_unenhanced():
        _enhance_artist(artist, client, conn, RepositoryFactory(conn))
        conn.commit()


@pytest.fixture(params=["script", "enrichment"])
def namer(request: pytest.FixtureRequest, script: ModuleType) -> Namer:
    """E1 and E3 hold whichever runs the naming."""
    if request.param == "script":
        return script.name_stubs
    return _enrich


def _stub(conn: Conn, mbid: str) -> str:
    stub_id = PgArtistRepository(conn).create_stub(mbid).id
    conn.commit()
    return stub_id


def _row(reader: Conn, artist_id: str) -> dict[str, Any]:
    row = reader.execute(
        """SELECT name, sort_name, disambiguation, normalized_name, mbid, needs_enhancement,
                  enhancement_error
           FROM artists WHERE id = %s""",
        (artist_id,),
    ).fetchone()
    assert row is not None
    return dict(row)


def _named(mbid: str) -> dict[str, Any]:
    """The row of a stub MusicBrainz has named from ``MB[mbid]``."""
    payload = MB[mbid]
    return {
        "name": payload["name"],
        "sort_name": payload["sort-name"],
        "disambiguation": payload["disambiguation"] or None,
        "normalized_name": normalize_artist(payload["name"]),
        "mbid": mbid,
        "needs_enhancement": False,
        "enhancement_error": None,
    }


def _unnamed(mbid: str) -> dict[str, Any]:
    """The row of a stub as S3 and ``create_stub`` make it."""
    return {
        "name": mbid,
        "sort_name": mbid,
        "disambiguation": None,
        "normalized_name": None,
        "mbid": mbid,
        "needs_enhancement": True,
        "enhancement_error": None,
    }


_NAMING = ("name", "sort_name", "disambiguation", "normalized_name", "mbid")


def _naming(row: dict[str, Any]) -> dict[str, Any]:
    """What naming sets, and the MBID it keeps."""
    return {column: row[column] for column in _NAMING}


def _lookups(client: FakeMbClient) -> list[str]:
    return sorted(call.removeprefix("lookup_artist:") for call in client.calls)


def test_stubs_are_named(script: ModuleType, conn: Conn, reader: Conn, queued: list[Any]) -> None:
    """S4: every artist whose name is still its MBID is named, even one enrichment before S4
    marked enhanced with its sort name and disambiguation filled; a named artist is neither
    looked up nor changed; the named stubs become matching candidates (S15); nothing is
    queued."""
    fresh = _stub(conn, BOC)
    marked = _stub(conn, KISS)
    conn.execute(  # as enrichment before S4 left it: everything but the name
        "UPDATE artists SET sort_name = 'KISS', disambiguation = 'US rock band' WHERE id = %s",
        (marked,),
    )
    PgArtistRepository(conn).mark_enhanced(marked)
    rush = PgArtistRepository(conn).upsert_musicbrainz_artist(
        RUSH, "Rush", "Rush", "rush", "Canadian rock band"
    )
    conn.commit()
    rush_before = _row(reader, rush)
    client = FakeMbClient(artists=MB)

    script.name_stubs(conn, client)

    assert _row(reader, fresh) == _named(BOC)
    assert _row(reader, marked) == _named(KISS)
    assert _row(reader, rush) == rush_before
    assert _lookups(client) == sorted([BOC, KISS])
    assert {fresh, marked} <= {artist.id for artist in PgArtistRepository(reader).list_all()}
    assert queued == []


def test_enrichment_names_a_stub(conn: Conn, reader: Conn) -> None:
    """S4: enrichment names a later stub (S6, S9, 2a) as it runs, and leaves the name of an
    artist that is no stub alone, even when MusicBrainz spells it otherwise."""
    stub = PgArtistRepository(conn).get_by_id(_stub(conn, BOC))
    held_id = PgArtistRepository(conn).upsert_musicbrainz_artist(
        RUSH, "Rush (band)", "Rush (band)", "rush band"
    )
    conn.commit()
    held = PgArtistRepository(conn).get_by_id(held_id)
    assert stub is not None and held is not None
    client = FakeMbClient(artists=MB)

    outcomes = [_enhance_artist(a, client, conn, RepositoryFactory(conn)) for a in (stub, held)]
    conn.commit()

    assert outcomes == [ArtistEnhanceOutcome.ENHANCED, ArtistEnhanceOutcome.ENHANCED]
    assert _row(reader, stub.id) == _named(BOC)
    after = _row(reader, held_id)
    assert (after["name"], after["normalized_name"]) == ("Rush (band)", "rush band")


def test_name_clash_keeps_normalized_null(
    namer: Namer, conn: Conn, reader: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E1: a name whose normalized form another artist holds (here a local artist, and the
    first of two stubs MusicBrainz names alike) is taken, with the normalized name left NULL;
    the holder is asked for by ``get_by_normalized_name`` first, a warning names the stub, and
    the holder is untouched."""
    holder = PgArtistRepository(conn).upsert_local_artist("Metallica", "metallica")
    clash = _stub(conn, METALLICA)
    twins = {_stub(conn, NIRVANA_US): NIRVANA_US, _stub(conn, NIRVANA_UK): NIRVANA_UK}
    conn.commit()
    holder_before = _row(reader, holder)
    asked: list[str] = []
    lookup = PgArtistRepository.get_by_normalized_name

    def _asking(self: PgArtistRepository, normalized_name: str) -> Artist | None:
        asked.append(normalized_name)
        return lookup(self, normalized_name)

    monkeypatch.setattr(PgArtistRepository, "get_by_normalized_name", _asking)

    with capture_logs() as events:
        namer(conn, FakeMbClient(artists=MB))

    assert _row(reader, clash) == {**_named(METALLICA), "normalized_name": None}
    assert _row(reader, holder) == holder_before
    rows = {stub_id: _row(reader, stub_id) for stub_id in twins}
    assert sorted(row["normalized_name"] or "" for row in rows.values()) == ["", "nirvana"]
    for stub_id, mbid in twins.items():
        assert rows[stub_id] == {
            **_named(mbid),
            "normalized_name": rows[stub_id]["normalized_name"],
        }
    (second,) = [stub_id for stub_id, row in rows.items() if row["normalized_name"] is None]
    warned = {e.get("artist_id") for e in events if e["log_level"] in ("warning", "error")}
    assert {clash, second} <= warned
    assert {"metallica", "nirvana"} <= set(asked)


def test_outage_resumes(script: ModuleType, conn: Conn, reader: Conn) -> None:
    """E2: a stub MusicBrainz could not answer for stays an untouched stub, with no error
    recorded, and the run returns (whether it went on to the rest or stopped there); what it
    named is committed. The next run names the stubs still unnamed, and looks up only those."""
    stubs = {_stub(conn, mbid): mbid for mbid in (BOC, RUSH, KISS)}

    script.name_stubs(conn, FakeMbClient(artists=MB, error_mbids={RUSH}))

    after_outage = {stub_id: _row(reader, stub_id) for stub_id in stubs}
    rush = next(stub_id for stub_id, mbid in stubs.items() if mbid == RUSH)
    assert after_outage[rush] == _unnamed(RUSH)
    for stub_id, mbid in stubs.items():
        assert after_outage[stub_id] in (_named(mbid), _unnamed(mbid))
    left = sorted(mbid for stub_id, mbid in stubs.items() if after_outage[stub_id]["name"] == mbid)

    client = FakeMbClient(artists=MB)
    script.name_stubs(conn, client)

    assert {stub_id: _row(reader, stub_id) for stub_id in stubs} == {
        stub_id: _named(mbid) for stub_id, mbid in stubs.items()
    }
    assert _lookups(client) == left


def test_404_and_malformed_recorded_once(namer: Namer, conn: Conn, reader: Conn) -> None:
    """E3: a 404 is recorded after one lookup; a malformed MBID is recorded with none (the
    fake answers a lookup of it with MusicBrainz's 400, which is no answer to record). Neither
    is looked up again, even once MusicBrainz would answer."""
    gone = _stub(conn, GONE)
    malformed = _stub(conn, MALFORMED)
    first = FakeMbClient(artists={}, bad_request_mbids={MALFORMED})

    namer(conn, first)

    recorded = {stub_id: _row(reader, stub_id) for stub_id in (gone, malformed)}
    for stub_id, mbid in ((gone, GONE), (malformed, MALFORMED)):
        assert recorded[stub_id]["enhancement_error"]
        assert _naming(recorded[stub_id]) == _naming(_unnamed(mbid))
    assert first.calls == [f"lookup_artist:{GONE}"]

    again = FakeMbClient(artists={GONE: _mb(GONE, "Back Again", "Back Again")})
    namer(conn, again)

    assert again.calls == []
    assert {stub_id: _row(reader, stub_id) for stub_id in (gone, malformed)} == recorded
