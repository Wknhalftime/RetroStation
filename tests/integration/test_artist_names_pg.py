"""Artist names come only from MusicBrainz names, aliases and tag spellings (phase 2a S10; C43;
AUD-R032), on PostgreSQL.

``artist_names`` holds them, and every write of ``artists.name`` adds a row. A log spelling is
refused: each cleaned spelling is one log name, already linked, and a wrong link would otherwise
confirm itself on every re-check. A row goes with its artist, because
``_delete_unused_local_artists`` deletes local artists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.artist_linking import PgArtistLinkingRepository
from backend.db.repositories.artist_names import PgArtistNameRepository
from backend.db.repositories.artists import PgArtistRepository
from backend.domain.catalog import ArtistLinkCandidate
from backend.domain.enums import ArtistNameSource
from backend.services.repository_factory import RepositoryFactory
from backend.services.shared_name_service import SharedName, apply_shared_name
from backend.tasks.mb_enrichment_tasks import _enhance_artist
from tests.fakes.mb_client import FakeMbClient

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def _names(conn: Conn, artist_id: str) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        """SELECT name, normalized_name, source FROM artist_names
            WHERE artist_id = %s ORDER BY source, name""",
        (artist_id,),
    ).fetchall()
    return [(r["name"], r["normalized_name"], r["source"]) for r in rows]


def _all_names(conn: Conn) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT name, source FROM artist_names ORDER BY name, source").fetchall()
    return [(r["name"], r["source"]) for r in rows]


def _local(conn: Conn, name: str = "Alice Cooper", normalized: str = "alice cooper") -> str:
    return PgArtistRepository(conn).upsert_local_artist(name, normalized)


def _mb(conn: Conn, mbid: str = ONE, name: str = "Alice Cooper") -> str:
    return PgArtistRepository(conn).upsert_musicbrainz_artist(
        mbid=mbid, name=name, sort_name=name, normalized_name="alice cooper"
    )


# --- the table ------------------------------------------------------------------------------------


def test_the_schema_after_0048(conn: Conn) -> None:
    columns = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in conn.execute(
            """SELECT column_name, data_type, is_nullable FROM information_schema.columns
                WHERE table_name = 'artist_names'"""
        ).fetchall()
    }
    assert columns["artist_id"] == ("text", "NO")
    assert columns["name"] == ("text", "NO")
    assert columns["normalized_name"] == ("text", "NO")
    assert columns["source"] == ("text", "NO")
    indexes = [
        r["indexdef"]
        for r in conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'artist_names'"
        ).fetchall()
    ]
    assert any("(normalized_name)" in d for d in indexes)


def test_a_log_spelling_is_refused(conn: Conn) -> None:
    artist = _local(conn)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        conn.execute(
            """INSERT INTO artist_names (artist_id, name, normalized_name, source)
               VALUES (%s, 'ALICE COOPER', 'alice cooper', 'log')""",
            (artist,),
        )


def test_the_port_has_no_source_for_a_log_spelling() -> None:
    assert {source.value for source in ArtistNameSource} == {
        "musicbrainz_name",
        "musicbrainz_alias",
        "tag",
    }


def test_each_source_the_table_allows(conn: Conn) -> None:
    artist = _local(conn)
    repo = PgArtistNameRepository(conn)

    assert repo.add(
        artist, "Vincent Furnier", "vincent furnier", ArtistNameSource.MUSICBRAINZ_ALIAS
    )
    assert repo.add(artist, "Alice Cooper", "alice cooper", ArtistNameSource.MUSICBRAINZ_NAME)

    assert ("Vincent Furnier", "musicbrainz_alias") in _all_names(conn)
    assert ("Alice Cooper", "musicbrainz_name") in _all_names(conn)


def test_a_name_goes_with_its_artist(conn: Conn) -> None:
    artist = _local(conn)
    assert _names(conn, artist)

    conn.execute("DELETE FROM artists WHERE id = %s", (artist,))

    assert _all_names(conn) == []


def test_a_repeat_adds_nothing(conn: Conn) -> None:
    artist = _local(conn)
    repo = PgArtistNameRepository(conn)

    assert repo.add(artist, "Vincent", "vincent", ArtistNameSource.MUSICBRAINZ_ALIAS) is True
    assert repo.add(artist, "Vincent", "vincent", ArtistNameSource.MUSICBRAINZ_ALIAS) is False

    assert _all_names(conn).count(("Vincent", "musicbrainz_alias")) == 1


def test_one_spelling_from_two_sources_is_two_rows(conn: Conn) -> None:
    artist = _local(conn)
    repo = PgArtistNameRepository(conn)

    repo.add(artist, "Alice Cooper", "alice cooper", ArtistNameSource.MUSICBRAINZ_NAME)

    assert [source for _, _, source in _names(conn, artist)] == ["musicbrainz_name", "tag"]


def test_two_artists_can_share_a_name(conn: Conn) -> None:
    band, singer = _mb(conn, ONE), _mb(conn, TWO)

    assert _names(conn, band) == _names(conn, singer)


# --- every writer -----------------------------------------------------------------------------


def _writes_local(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    return _local(conn), ("Alice Cooper", "alice cooper", "tag")


def _writes_a_new_mb_artist(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    return _mb(conn), ("Alice Cooper", "alice cooper", "musicbrainz_name")


def _writes_a_promotion(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    _local(conn, "ALICE COOPER")
    return _mb(conn), ("Alice Cooper", "alice cooper", "musicbrainz_name")


def _writes_a_further_credit(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    artist = PgArtistRepository(conn).upsert_further_credit(
        ONE, "Alice Cooper", "Cooper, Alice", "alice cooper"
    )
    return artist, ("Alice Cooper", "alice cooper", "musicbrainz_name")


def _writes_a_link(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    artist = _local(conn, "ALICE COOPER")
    linked = PgArtistLinkingRepository(conn).link(
        artist, ArtistLinkCandidate(ONE, "Alice Cooper", "Cooper, Alice", None)
    )
    assert linked is True
    return artist, ("Alice Cooper", "alice cooper", "musicbrainz_name")


def _writes_a_stub_name(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    catalog = PgArtistRepository(conn)
    stub = catalog.create_stub(ONE)
    client = FakeMbClient(artists={ONE: {"id": ONE, "name": "Alice Cooper"}})
    _enhance_artist(stub, client, conn, RepositoryFactory(conn))
    return stub.id, ("Alice Cooper", "alice cooper", "musicbrainz_name")


def _writes_the_sweep(conn: Conn) -> tuple[str, tuple[str, str, str]]:
    """The spelling comes from tags, so that is its source."""
    repos = RepositoryFactory(conn)
    apply_shared_name(
        SharedName("alice cooper", {ONE: "Alice Cooper"}, ()),
        repos.artists,
        repos.library_suggestions,
    )
    artist = repos.artists.get_by_mbid(ONE)
    assert artist is not None
    return artist.id, ("Alice Cooper", "alice cooper", "tag")


@pytest.mark.parametrize(
    "writer",
    [
        _writes_local,
        _writes_a_new_mb_artist,
        _writes_a_promotion,
        _writes_a_further_credit,
        _writes_a_link,
        _writes_a_stub_name,
        _writes_the_sweep,
    ],
    ids=lambda writer: writer.__name__.removeprefix("_writes_"),
)
def test_every_writer_adds_a_name(
    conn: Conn, writer: Callable[[Conn], tuple[str, tuple[str, str, str]]]
) -> None:
    artist, expected = writer(conn)

    assert expected in _names(conn, artist)


def test_a_promotion_keeps_the_tag_spelling_it_replaced(conn: Conn) -> None:
    artist, _ = _writes_a_promotion(conn)

    assert _names(conn, artist) == [
        ("Alice Cooper", "alice cooper", "musicbrainz_name"),
        ("ALICE COOPER", "alice cooper", "tag"),
    ]


def test_finding_an_artist_adds_nothing(conn: Conn) -> None:
    """Only a write of ``artists.name`` adds a row: the name wasn't written here."""
    _mb(conn)
    _local(conn, "Other", "other")
    before = _all_names(conn)

    _mb(conn, ONE, "Another Spelling")
    _local(conn, "OTHER", "other")

    assert _all_names(conn) == before


def test_an_unnamed_stub_has_no_name(conn: Conn) -> None:
    """Its name is its MBID until enrichment names it."""
    PgArtistRepository(conn).create_stub(ONE)

    assert _all_names(conn) == []


def test_a_refused_link_adds_nothing(conn: Conn) -> None:
    artist = _local(conn, "Other", "other")
    _mb(conn)
    before = _all_names(conn)

    linked = PgArtistLinkingRepository(conn).link(
        artist, ArtistLinkCandidate(ONE, "Alice Cooper", "Cooper, Alice", None)
    )

    assert linked is False
    assert _all_names(conn) == before


def test_a_symbol_only_stub_name_keeps_its_row(conn: Conn) -> None:
    """It normalizes to nothing, and the name is still the one MusicBrainz gave."""
    catalog = PgArtistRepository(conn)
    stub = catalog.create_stub(ONE)
    client: Any = FakeMbClient(artists={ONE: {"id": ONE, "name": "!!!"}})

    _enhance_artist(stub, client, conn, RepositoryFactory(conn))

    assert _names(conn, stub.id) == [("!!!", "", "musicbrainz_name")]


def test_sort_name_or_disambiguation_alone_adds_nothing(conn: Conn) -> None:
    """Enrichment on an artist that is no stub writes no name, so it adds none: this artist has
    no row, as one from before 0048 hasn't until the backfill."""
    artist = _mb(conn)
    conn.execute("DELETE FROM artist_names")
    catalog = PgArtistRepository(conn)
    held = catalog.get_by_id(artist)
    assert held is not None
    before = _all_names(conn)
    assert before == []
    client: Any = FakeMbClient(
        artists={ONE: {"id": ONE, "name": "Other", "sort-name": "Cooper", "disambiguation": "x"}}
    )

    _enhance_artist(held, client, conn, RepositoryFactory(conn))

    assert _all_names(conn) == before
