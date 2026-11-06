"""The library suggestions' port, its fake against its PostgreSQL adapter (artist model phase 2a,
SPEC S13, S14; C21, section 6.10).

``LibrarySuggestionRepository`` holds what a later page asks Lance about a library artist. In 2a
that means rows naming no file, one per name and reason:

- ``upsert_for_name(name, reason, candidates, evidence)``: a new row is ``open``. A repeat updates
  the candidates and the evidence and never ``state``, so a name Lance kept separate never reopens
  (phase 5's E5). It says whether it wrote anything: writing the same thing again writes nothing.
- ``get_for_name(name, reason)``: the row, or None.

On PostgreSQL the port writes in the caller's transaction and commits nothing (C75).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import psycopg
import pytest
from psycopg.rows import dict_row

from backend.domain.enums import SuggestionReason, SuggestionState
from backend.domain.library import LibrarySuggestion
from backend.repositories.library_suggestions import LibrarySuggestionRepository
from backend.services.repository_factory import RepositoryFactory
from tests.fakes.library_suggestions import FakeLibrarySuggestionRepository

SHARED = SuggestionReason.SHARED_NAME
ACTS = SuggestionReason.DIFFERENT_ACTS


@dataclass
class _Ports:
    suggestions: LibrarySuggestionRepository
    set_state: Callable[[str, SuggestionReason, SuggestionState], None]


@pytest.fixture(params=["fake", pytest.param("pg", marks=pytest.mark.integration)])
def ports(request: pytest.FixtureRequest) -> Iterator[_Ports]:
    if request.param == "fake":
        fake = FakeLibrarySuggestionRepository()
        yield _Ports(fake, fake.set_state)
        return
    migrated_db: str = request.getfixturevalue("migrated_db")
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:

        def set_state(name: str, reason: SuggestionReason, state: SuggestionState) -> None:
            conn.execute(
                """UPDATE library_suggestions SET state = %s
                    WHERE normalized_name = %s AND reason = %s""",
                (state.value, name, reason.value),
            )

        yield _Ports(RepositoryFactory(conn).library_suggestions, set_state)
        conn.rollback()


def test_a_new_row_is_open(ports: _Ports) -> None:
    written = ports.suggestions.upsert_for_name(
        "alice cooper", SHARED, ["mbid-a", "mbid-b"], {"shared_titles": ["Poison"]}
    )

    assert written is True
    assert ports.suggestions.get_for_name("alice cooper", SHARED) == LibrarySuggestion(
        normalized_name="alice cooper",
        reason=SHARED,
        candidates=("mbid-a", "mbid-b"),
        evidence={"shared_titles": ["Poison"]},
        state=SuggestionState.OPEN,
    )


def test_an_unknown_row_is_none(ports: _Ports) -> None:
    ports.suggestions.upsert_for_name("alice cooper", SHARED, [], {})

    assert ports.suggestions.get_for_name("alice cooper", ACTS) is None
    assert ports.suggestions.get_for_name("nirvana", SHARED) is None


def test_one_row_per_name_and_reason(ports: _Ports) -> None:
    ports.suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a"], {})
    ports.suggestions.upsert_for_name("alice cooper", ACTS, ["mbid-b"], {})
    ports.suggestions.upsert_for_name("nirvana", SHARED, ["mbid-c"], {})

    rows = [
        ports.suggestions.get_for_name("alice cooper", SHARED),
        ports.suggestions.get_for_name("alice cooper", ACTS),
        ports.suggestions.get_for_name("nirvana", SHARED),
    ]
    assert [row.candidates if row else None for row in rows] == [
        ("mbid-a",),
        ("mbid-b",),
        ("mbid-c",),
    ]


def test_a_repeat_updates_candidates_and_evidence(ports: _Ports) -> None:
    ports.suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a"], {"shared_titles": []})

    written = ports.suggestions.upsert_for_name(
        "alice cooper", SHARED, ["mbid-a", "mbid-b"], {"shared_titles": ["Poison"]}
    )

    assert written is True
    row = ports.suggestions.get_for_name("alice cooper", SHARED)
    assert row is not None
    assert (row.candidates, row.evidence) == (
        ("mbid-a", "mbid-b"),
        {"shared_titles": ["Poison"]},
    )


@pytest.mark.parametrize("state", [SuggestionState.ACCEPTED, SuggestionState.DISMISSED])
def test_a_repeat_never_changes_the_state(ports: _Ports, state: SuggestionState) -> None:
    ports.suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a"], {})
    ports.set_state("alice cooper", SHARED, state)

    ports.suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a", "mbid-b"], {})

    row = ports.suggestions.get_for_name("alice cooper", SHARED)
    assert row is not None
    assert (row.candidates, row.state) == (("mbid-a", "mbid-b"), state)


def test_writing_the_same_thing_again_writes_nothing(ports: _Ports) -> None:
    evidence = {"shared_titles": ["Poison"]}
    ports.suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a"], evidence)

    assert ports.suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a"], evidence) is False


@pytest.mark.integration
def test_the_adapter_commits_nothing(migrated_db: str) -> None:
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        suggestions = RepositoryFactory(conn).library_suggestions
        suggestions.upsert_for_name("alice cooper", SHARED, ["mbid-a"], {})
        conn.rollback()

        assert suggestions.get_for_name("alice cooper", SHARED) is None
