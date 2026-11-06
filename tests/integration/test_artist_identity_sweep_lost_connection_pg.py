"""A lost connection costs a scan nothing (phase 2a S14, reading 8), on PostgreSQL.

The sweep runs after the scan's work is committed. A connection the server drops then must not
fail the scan: its progress would say FAILED with everything written, and its hand-off to
enrichment would be skipped. A dropped connection reaches the sweep two ways, as a
``LibraryStorageError`` from the adapters that translate it and as a ``psycopg.OperationalError``
from those that don't, and a rollback on it raises too. The next sweep does the work.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from structlog.testing import capture_logs

from backend.db.repositories.artist_tag_files import PgArtistTagFilesRepository
from backend.db.repositories.library_suggestions import PgLibrarySuggestionRepository
from backend.domain.enums import SuggestionReason
from backend.domain.library import ArtistTagGroup
from backend.services.normalization import normalize_artist
from backend.services.repository_factory import RepositoryFactory
from backend.tasks.artist_identity_tasks import ArtistIdentitySweep, sweep_artist_identities

pytestmark = pytest.mark.integration

type Conn = psycopg.Connection[DictRow]

BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"
SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


@pytest.fixture
def server(migrated_db: str) -> Iterator[Conn]:
    """A second connection, to drop the first from."""
    with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as connection:
        yield connection


def _shared_name(server: Conn, name: str = "Alice Cooper") -> None:
    for tag in (BAND, SINGER):
        server.execute(
            """INSERT INTO library_files
                   (file_path, format, track_title, artist_name, normalized_artist_name,
                    artist_mbid, file_status)
               VALUES (%s, 'flac', 'Some Song', %s, %s, %s, 'present')""",
            (f"/music/{uuid4().hex}.flac", name, normalize_artist(name), tag),
        )


def _drop(server: Conn, conn: Conn) -> None:
    server.execute("SELECT pg_terminate_backend(%s)", (conn.info.backend_pid,))


def test_dropped_before_the_read(conn: Conn, server: Conn) -> None:
    _shared_name(server)
    repos = RepositoryFactory(conn)
    _drop(server, conn)

    with capture_logs() as events:
        result = sweep_artist_identities(conn, repos)

    assert result is None
    assert [e["log_level"] for e in events] == ["warning"]


def test_dropped_with_a_read_still_open(conn: Conn, server: Conn) -> None:
    """A caller's last read leaves a transaction open, so the first commit is what meets it."""
    _shared_name(server)
    repos = RepositoryFactory(conn)
    conn.execute("SELECT 1")
    _drop(server, conn)

    with capture_logs() as events:
        result = sweep_artist_identities(conn, repos)

    assert result is None
    assert [e["log_level"] for e in events] == ["warning"]


def test_dropped_as_the_read_ends(
    conn: Conn, server: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit that ends the read meets it, so this is still a failed read."""
    _shared_name(server)
    read = PgArtistTagFilesRepository.list_tag_groups

    def read_then_drop(self: PgArtistTagFilesRepository) -> list[ArtistTagGroup]:
        groups = read(self)
        _drop(server, conn)
        return groups

    monkeypatch.setattr(PgArtistTagFilesRepository, "list_tag_groups", read_then_drop)

    assert sweep_artist_identities(conn, RepositoryFactory(conn)) is None


def test_dropped_during_a_names_writes(
    conn: Conn, server: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each name fails alone and is counted. The first name's commit meets the drop, and the
    second's first read comes from the catalog's adapter, which doesn't translate; neither
    handler's rollback may raise on the closed connection. Nothing lands, and nothing fails."""
    _shared_name(server)
    _shared_name(server, "Bob Seger")
    write = PgLibrarySuggestionRepository.upsert_for_name

    def write_then_drop(
        self: PgLibrarySuggestionRepository,
        normalized_name: str,
        reason: SuggestionReason,
        candidates: Sequence[str],
        evidence: Mapping[str, object],
    ) -> bool:
        written = write(self, normalized_name, reason, candidates, evidence)
        _drop(server, conn)
        return written

    monkeypatch.setattr(PgLibrarySuggestionRepository, "upsert_for_name", write_then_drop)

    with capture_logs() as events:
        result = sweep_artist_identities(conn, RepositoryFactory(conn))

    assert result == ArtistIdentitySweep(names=2, artists_made=0, rows_written=0, names_failed=2)
    warned = {e.get("normalized_name") for e in events if e["log_level"] == "warning"}
    assert warned == {normalize_artist("Alice Cooper"), normalize_artist("Bob Seger")}
    rows = server.execute("SELECT count(*) AS n FROM library_suggestions").fetchone()
    assert rows is not None
    assert rows["n"] == 0
