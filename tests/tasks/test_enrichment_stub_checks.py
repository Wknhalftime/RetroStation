"""Enrichment's stub checks beyond the locked naming tests (artist model phase 1a, PR 2; E3).

The locked ``test_enrichment_names_stubs.py`` runs ``_enhance_artist`` alone. Queued enrichment
first looks up every pending MBID in one pre-pass (``coalesce_artist_lookups``), so a malformed
stub MBID must be left out there too: it is never sent to MusicBrainz, only recorded. A stub
MusicBrainz gives no name for is recorded the same way, so the naming script does not count it
named and pick it again on every run.

MusicBrainz is ``FakeMbClient``; the catalog is PostgreSQL, read back on a second connection.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from backend.config import get_settings
from backend.db.repositories.artists import PgArtistRepository
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.mb_enrichment_tasks import (
    ArtistEnhanceOutcome,
    _enhance_artist,
    _PhaseContext,
    _run_artist_phase,
)
from tests.fakes.mb_client import FakeMbClient
from tests.fakes.task_progress import FakeTaskProgressRepository

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

GOOD = "b0000000-0000-4000-8000-000000000001"
MALFORMED = "artist-mbid-0002"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def _stub(conn: Conn, mbid: str) -> str:
    stub_id = PgArtistRepository(conn).create_stub(mbid).id
    conn.commit()
    return stub_id


def _row(migrated_db: str, artist_id: str) -> dict[str, Any]:
    with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as reader:
        row = reader.execute(
            "SELECT name, sort_name, normalized_name, needs_enhancement, enhancement_error"
            " FROM artists WHERE id = %s",
            (artist_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def test_the_lookup_pre_pass_never_sends_a_malformed_stub_mbid(
    conn: Conn, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, malformed = _stub(conn, GOOD), _stub(conn, MALFORMED)
    client = FakeMbClient(
        artists={GOOD: {"id": GOOD, "name": "Hedder", "sort-name": "Hedder"}},
        bad_request_mbids={MALFORMED},
    )
    monkeypatch.setattr(
        "backend.tasks.mb_enrichment_tasks.MusicBrainzApiClient",
        lambda *_args, **_kwargs: contextlib.nullcontext(client),
    )
    pending = PgArtistRepository(conn).list_unenhanced()
    ctx = _PhaseContext(
        task_id="stub-checks",
        task_started_at=datetime.now(tz=UTC),
        total=len(pending),
        progress_repo=FakeTaskProgressRepository(),
    )
    settings = get_settings().model_copy(update={"database_url": migrated_db})

    _run_artist_phase(pending, ctx, settings)

    assert client.calls == [f"lookup_artist:{GOOD}"]
    assert _row(migrated_db, good)["name"] == "Hedder"
    row = _row(migrated_db, malformed)
    assert (row["name"], row["enhancement_error"]) == (MALFORMED, f"malformed MBID: {MALFORMED!r}")
    assert (ctx.done["artists"], ctx.failed["artists"]) == (1, 1)


@pytest.mark.parametrize("name", [None, "", "   "])
def test_a_stub_musicbrainz_gives_no_name_is_recorded(
    conn: Conn, migrated_db: str, name: str | None
) -> None:
    stub_id = _stub(conn, GOOD)
    payload: dict[str, Any] = {"id": GOOD, "sort-name": "Sorted", "disambiguation": "a band"}
    if name is not None:
        payload["name"] = name
    client = FakeMbClient(artists={GOOD: payload})
    stub = PgArtistRepository(conn).get_by_id(stub_id)
    assert stub is not None

    outcome = _enhance_artist(stub, client, conn, RepositoryFactory(conn))
    conn.commit()

    assert outcome is ArtistEnhanceOutcome.FAILED
    assert _row(migrated_db, stub_id) == {
        "name": GOOD,
        "sort_name": GOOD,
        "normalized_name": None,
        "needs_enhancement": True,
        "enhancement_error": f"MB lookup gave no name for stub mbid={GOOD}",
    }
