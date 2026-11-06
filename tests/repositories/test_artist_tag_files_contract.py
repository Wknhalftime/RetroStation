"""The sweep's read of ``library_files``, its fake against its PostgreSQL adapter (artist model
phase 2a, SPEC S14; M33, D6).

``ArtistTagFilesRepository.list_tag_groups`` is one grouped read of present files, for the names
whose files carry two or more different artist-MBID tag values, compared without case: one group
per (normalized name, tag value, spelling, track title), with its file count. A missing file, an
untagged file and a blank name are not read. A name with one value is left out, so the read stays
small. The values are raw: the sweep decides which are real MBIDs, so a joined value and a
placeholder come back too.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.library import ArtistTagGroup
from backend.repositories.artist_tag_files import ArtistTagFilesRepository
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.artist_tag_files import FakeArtistTagFilesRepository, FakeTagFile

BAND = "4d7928cd-7ed2-4282-8c29-c0c9f966f1bd"
SINGER = "ee58c59f-8e7f-4430-b8ca-236c4d3745ae"


@dataclass
class _Ports:
    files: ArtistTagFilesRepository
    add: Callable[[FakeTagFile], None]


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def ports(request: pytest.FixtureRequest) -> Iterator[_Ports]:
    if request.param == "fake":
        fake = FakeArtistTagFilesRepository()
        yield _Ports(fake, fake.files.append)
        return
    migrated_db: str = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:

        def add(file: FakeTagFile) -> None:
            conn.execute(
                """INSERT INTO library_files
                       (file_path, format, track_title, artist_name, normalized_artist_name,
                        artist_mbid, file_status, missing_since)
                   VALUES (%s, 'flac', %s, %s, %s, %s, %s, %s)""",
                (
                    f"/music/{uuid4().hex}.flac",
                    file.track_title,
                    file.artist_name,
                    file.normalized_name,
                    file.tag_value,
                    "present" if file.present else "missing",
                    None if file.present else "2026-10-01T00:00:00Z",
                ),
            )

        yield _Ports(RepositoryFactory(conn).artist_tag_files, add)
        conn.rollback()


def _file(tag: str | None, **fields: object) -> FakeTagFile:
    values: dict[str, object] = {"normalized_name": "alice cooper", "tag_value": tag, **fields}
    return FakeTagFile(**values)  # type: ignore[arg-type]


def test_a_name_with_one_value_is_left_out(ports: _Ports) -> None:
    ports.add(_file(BAND))
    ports.add(_file(BAND))
    ports.add(_file(BAND.upper()))
    ports.add(_file(None))

    assert ports.files.list_tag_groups() == []


def test_groups_carry_their_spelling_title_and_count(ports: _Ports) -> None:
    ports.add(_file(BAND, artist_name="Alice Cooper", track_title="Poison"))
    ports.add(_file(BAND, artist_name="Alice Cooper", track_title="Poison"))
    ports.add(_file(BAND, artist_name="ALICE COOPER", track_title="Poison"))
    ports.add(_file(SINGER, artist_name="Alice Cooper", track_title=None))

    assert set(ports.files.list_tag_groups()) == {
        ArtistTagGroup("alice cooper", BAND, "Alice Cooper", "Poison", 2),
        ArtistTagGroup("alice cooper", BAND, "ALICE COOPER", "Poison", 1),
        ArtistTagGroup("alice cooper", SINGER, "Alice Cooper", None, 1),
    }


def test_the_values_come_back_raw(ports: _Ports) -> None:
    """The port doesn't decide what an MBID is: a joined value and a placeholder come back."""
    joined = f"{BAND}, {SINGER}"
    ports.add(_file(BAND))
    ports.add(_file(joined))

    groups = ports.files.list_tag_groups()

    assert {group.tag_value for group in groups} == {BAND, joined}


def test_a_missing_file_makes_no_name_shared(ports: _Ports) -> None:
    ports.add(_file(BAND))
    ports.add(_file(SINGER, present=False))

    assert ports.files.list_tag_groups() == []


def test_a_missing_file_is_not_read_on_a_shared_name(ports: _Ports) -> None:
    third = "0a000000-0000-4000-8000-0000000000c3"
    ports.add(_file(BAND))
    ports.add(_file(SINGER))
    ports.add(_file(third, present=False))

    assert {group.tag_value for group in ports.files.list_tag_groups()} == {BAND, SINGER}


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_name_is_skipped(ports: _Ports, blank: str | None) -> None:
    ports.add(_file(BAND, normalized_name=blank))
    ports.add(_file(SINGER, normalized_name=blank))

    assert ports.files.list_tag_groups() == []


def test_names_are_kept_apart(ports: _Ports) -> None:
    ports.add(_file(BAND))
    ports.add(_file(SINGER, normalized_name="vincent furnier"))

    assert ports.files.list_tag_groups() == []
