"""The files' artist columns: fake against PostgreSQL (phase 2a S1, S3, S10, S14).

A new port, because the library-files fake is locked. The rows to fill are those with
``raw_metadata`` whose columns are unfilled or hold an unnamed credit, in id order from a cursor. A
group is one (MBID, name) across the present files holding several MBIDs. A tag spelling is any
credit's named pair, on any file. The adapter commits nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row
from psycopg.types.json import Jsonb

from backend.db.repositories.artist_credit_files import PgArtistCreditFilesRepository
from backend.domain.library import CreditEntry, CreditGroup
from backend.repositories.artist_credit_files import ArtistCreditFilesRepository
from tests.fakes.artist_credit_files import FakeArtistCreditFilesRepository

ONE = "0a000000-0000-4000-8000-000000000001"
TWO = "0a000000-0000-4000-8000-000000000002"
THREE = "0a000000-0000-4000-8000-000000000003"
FOUR = "0a000000-0000-4000-8000-000000000004"
FIVE = "0a000000-0000-4000-8000-000000000005"
SIX = "0a000000-0000-4000-8000-000000000006"
UNKNOWN = "125ec42a-7229-4250-afc5-e057484327fe"  # MusicBrainz's [unknown]
RAW = {"artist": "['A']"}
NAMED = (CreditEntry(ONE, "A", " & "), CreditEntry(TWO, "B", ""))
UNNAMED = (CreditEntry(ONE), CreditEntry(TWO))


class _PgRows:
    """The port on a real table, and a way to seed it that the fake also has."""

    def __init__(self, conn: psycopg.Connection[DictRow]) -> None:
        self.conn = conn
        self.repo = PgArtistCreditFilesRepository(conn)

    def add(
        self,
        *,
        raw_metadata: dict[str, Any] | None = RAW,
        artist_mbids: tuple[str, ...] | None = None,
        credit: tuple[CreditEntry, ...] | None = None,
        present: bool = True,
        recording_mbid: str | None = None,
        format: str = "flac",  # noqa: A002
        file_path: str | None = None,
    ) -> UUID:
        row = self.conn.execute(
            """INSERT INTO library_files
                   (file_path, format, raw_metadata, artist_mbids, credit, file_status,
                    missing_since, recording_mbid)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (
                file_path or f"/music/{uuid4().hex}.flac",
                format,
                Jsonb(raw_metadata) if raw_metadata is not None else None,
                None if artist_mbids is None else list(artist_mbids),
                None if credit is None else Jsonb([_json(e) for e in credit]),
                "present" if present else "missing",
                None if present else "2026-10-01T00:00:00Z",
                recording_mbid,
            ),
        ).fetchone()
        assert row is not None
        return UUID(str(row["id"]))

    def columns(self, file_id: UUID) -> tuple[list[str] | None, list[dict[str, Any]] | None]:
        row = self.conn.execute(
            "SELECT artist_mbids, credit FROM library_files WHERE id = %s", (file_id,)
        ).fetchone()
        assert row is not None
        return row["artist_mbids"], row["credit"]


class _FakeRows:
    def __init__(self) -> None:
        self.repo = FakeArtistCreditFilesRepository()

    def add(
        self,
        *,
        raw_metadata: dict[str, Any] | None = RAW,
        artist_mbids: tuple[str, ...] | None = None,
        credit: tuple[CreditEntry, ...] | None = None,
        present: bool = True,
        recording_mbid: str | None = None,
        format: str = "flac",  # noqa: A002
        file_path: str | None = None,
    ) -> UUID:
        return self.repo.add(
            raw_metadata=raw_metadata,
            artist_mbids=artist_mbids,
            credit=credit,
            present=present,
            recording_mbid=recording_mbid,
            format=format,
            file_path=file_path or f"/music/{uuid4().hex}.flac",
        ).id

    def columns(self, file_id: UUID) -> tuple[list[str] | None, list[dict[str, Any]] | None]:
        [file] = [f for f in self.repo.files if f.id == file_id]
        return (
            None if file.artist_mbids is None else list(file.artist_mbids),
            None if file.credit is None else [_json(e) for e in file.credit],
        )


def _json(entry: CreditEntry) -> dict[str, Any]:
    return {"name": entry.name, "mbid": entry.mbid, "join": entry.join}


@pytest.fixture(params=["fake", "pg"])
def rows(request: pytest.FixtureRequest) -> Iterator[Any]:
    if request.param == "fake":
        yield _FakeRows()
        return
    migrated_db = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield _PgRows(conn)
        conn.rollback()


def _ids(
    repo: ArtistCreditFilesRepository, after: UUID | None = None, limit: int = 10
) -> set[UUID]:
    return {file.id for file in repo.list_to_fill(after, limit)}


@pytest.mark.integration
def test_rows_to_fill(rows: Any) -> None:
    unfilled = rows.add()
    missing = rows.add(present=False)
    unnamed = rows.add(artist_mbids=(ONE, TWO), credit=UNNAMED)
    partly = rows.add(artist_mbids=(ONE, TWO), credit=(CreditEntry(ONE, "A"), CreditEntry(TWO)))
    rows.add(raw_metadata=None)
    rows.add(artist_mbids=(ONE, TWO), credit=NAMED)
    rows.add(artist_mbids=(), credit=None)

    assert _ids(rows.repo) == {unfilled, missing, unnamed, partly}


@pytest.mark.integration
def test_a_row_to_fill_says_what_the_backfill_needs(rows: Any) -> None:
    file_id = rows.add(
        raw_metadata={"TPE1": "A"},
        recording_mbid=ONE,
        format="mp3",
        file_path="/music/fixed.mp3",
    )

    [file] = rows.repo.list_to_fill(None, 10)

    assert (file.id, file.file_path, file.format) == (file_id, "/music/fixed.mp3", "mp3")
    assert (file.raw_metadata, file.recording_mbid) == ({"TPE1": "A"}, ONE)


@pytest.mark.integration
def test_the_cursor_walks_in_id_order(rows: Any) -> None:
    added = sorted(rows.add() for _ in range(5))

    first = rows.repo.list_to_fill(None, 2)
    second = rows.repo.list_to_fill(first[-1].id, 2)
    third = rows.repo.list_to_fill(second[-1].id, 2)
    last = rows.repo.list_to_fill(third[-1].id, 2)

    assert [f.id for f in [*first, *second, *third]] == added
    assert last == []


@pytest.mark.integration
def test_writing_fills_both_columns(rows: Any) -> None:
    file_id = rows.add()

    rows.repo.write_credit(file_id, (ONE, TWO), NAMED)

    assert rows.columns(file_id) == (
        [ONE, TWO],
        [{"name": "A", "mbid": ONE, "join": " & "}, {"name": "B", "mbid": TWO, "join": ""}],
    )
    assert _ids(rows.repo) == set()


@pytest.mark.integration
def test_writing_an_untagged_row(rows: Any) -> None:
    file_id = rows.add()

    rows.repo.write_credit(file_id, (), None)

    assert rows.columns(file_id) == ([], None)
    assert _ids(rows.repo) == set()


@pytest.mark.integration
def test_writing_keeps_null_names_and_joins(rows: Any) -> None:
    file_id = rows.add()

    rows.repo.write_credit(file_id, (ONE, TWO), UNNAMED)

    assert rows.columns(file_id)[1] == [
        {"name": None, "mbid": ONE, "join": None},
        {"name": None, "mbid": TWO, "join": None},
    ]
    assert _ids(rows.repo) == {file_id}


@pytest.mark.integration
def test_credit_groups_count_present_files_holding_several(rows: Any) -> None:
    """Only those: a single-MBID file's spelling would name its MBID, and S6 would promote the
    name-only artist from tags alone. A placeholder beside one MBID isn't several (D6)."""
    rows.add(artist_mbids=(ONE, TWO), credit=NAMED)
    rows.add(artist_mbids=(ONE, TWO), credit=NAMED)
    rows.add(artist_mbids=(ONE, TWO), credit=UNNAMED)
    rows.add(artist_mbids=(ONE, TWO), credit=NAMED, present=False)
    rows.add(artist_mbids=(ONE,), credit=(CreditEntry(ONE, "Single", ""),))
    rows.add(artist_mbids=(ONE,), credit=(CreditEntry(ONE, "Gone", ""),), present=False)
    rows.add(artist_mbids=(THREE,), credit=(CreditEntry(THREE, "Alone", ""),))
    rows.add(
        artist_mbids=(FOUR, UNKNOWN),
        credit=(CreditEntry(FOUR, "Beside", " & "), CreditEntry(UNKNOWN, "[unknown]", "")),
    )
    rows.add(
        artist_mbids=(FIVE, SIX),
        credit=(CreditEntry(FIVE, "Five", " & "), CreditEntry(SIX, "Six", "")),
        present=False,
    )
    rows.add(artist_mbids=(), credit=None)
    rows.add()

    assert sorted(rows.repo.list_credit_groups(), key=_group_key) == sorted(
        [
            CreditGroup(ONE, "A", 2),
            CreditGroup(TWO, "B", 2),
            CreditGroup(ONE, None, 1),
            CreditGroup(TWO, None, 1),
        ],
        key=_group_key,
    )


def _group_key(group: CreditGroup) -> tuple[str, str]:
    return (group.mbid, group.name or "")


@pytest.mark.integration
def test_tag_spellings_are_every_named_pair(rows: Any) -> None:
    rows.add(artist_mbids=(ONE, TWO), credit=NAMED)
    rows.add(artist_mbids=(ONE, TWO), credit=UNNAMED)
    rows.add(artist_mbids=(ONE,), credit=(CreditEntry(ONE, "Single", ""),), present=False)
    rows.add(artist_mbids=(), credit=None)

    assert rows.repo.list_tag_spellings() == {(ONE, "A"), (TWO, "B"), (ONE, "Single")}


@pytest.mark.integration
def test_the_adapter_commits_nothing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        pg = _PgRows(conn)
        file_id = pg.add()
        conn.commit()

        pg.repo.write_credit(file_id, (ONE,), (CreditEntry(ONE, "A", ""),))

        with psycopg.connect(migrated_db, row_factory=dict_row, autocommit=True) as other:
            row = other.execute(
                "SELECT artist_mbids FROM library_files WHERE id = %s", (file_id,)
            ).fetchone()
        assert row is not None
        assert row["artist_mbids"] is None
        conn.rollback()
