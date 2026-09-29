"""Guard test: the schedule reader resolves files PER PLAY (spec: Data, Day window; D17, D19).

The reader joins curation's view ``play_file_resolution`` through a fenced lateral subquery,
``LEFT JOIN LATERAL (... WHERE play_event_id = ... OFFSET 0)``, so the view is evaluated once
for each play of the day and never for every play in the database (on dev: about 10 ms fenced,
about 10 s unfenced). Every other reader test passes either way; only the plan tells the two
apart, so this test reads the plan of the statement the reader actually runs.

Per play means: the view's own tables (``matches``, ``song_masters``, ``format_overrides``)
sit on the inner side of a Nested Loop whose outer side yields the day's plays, and each
``play_events`` scan on that inner side looks the play up by ``id``, a value taken from the
outer row. A Hash or Merge join of the whole view, or a full scan of ``play_events`` beside
it, is the unfenced shape.

Deterministic whatever the table sizes: the plan is also taken with ``enable_nestloop = off``.
The fence leaves the planner no other way to run the lateral reference, so the fenced plan
keeps its per-play Nested Loop; an unfenced join is free to hash the whole view, and does. With
default settings a small test database can give an unfenced join the per-play shape too, so
that case alone would not catch a lost fence.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.abc import Params
from psycopg.rows import DictRow, dict_row

from backend.db.repositories.playable_schedule import PgPlayableScheduleRepository
from tests.integration.stream_seed import DAY, Conn

type PlanNode = Mapping[str, object]

VIEW_TABLES = frozenset({"matches", "song_masters", "format_overrides"})
"""Tables only ``play_file_resolution`` reads: where they sit in the plan is where the view is."""

PLAYS = "play_events"
BY_ID = re.compile(r"\bid = ")
CONDITIONS = ("Index Cond", "Recheck Cond", "Filter")

PLANNER_SETTINGS: dict[str, dict[str, str]] = {
    "default planner": {},
    "nested loops disabled": {"enable_nestloop": "off"},
}


class StatementRecorder:
    """Stands in for the reader's connection: runs each statement and remembers it."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn
        self.statements: list[tuple[str, Params | None]] = []

    def execute(self, query: str, params: Params | None = None) -> psycopg.Cursor[DictRow]:
        self.statements.append((query, params))
        return self._conn.execute(query, params)


@pytest.fixture
def conn(migrated_db: str) -> Iterator[Conn]:
    with psycopg.connect(migrated_db, row_factory=dict_row) as connection:
        yield connection


def reader_statements(conn: Conn) -> list[tuple[str, Params | None]]:
    """The statements ``get_day`` runs for one station-day."""
    recorder = StatementRecorder(conn)
    PgPlayableScheduleRepository(cast(Conn, recorder)).get_day(uuid4(), DAY)
    return recorder.statements


def plan_of(conn: Conn, query: str, params: Params | None) -> PlanNode:
    row = conn.execute("EXPLAIN (FORMAT JSON) " + query, params).fetchone()
    assert row is not None
    document = row["QUERY PLAN"]
    assert isinstance(document, list)
    plan = document[0]["Plan"]
    assert isinstance(plan, dict)
    return plan


def children(node: PlanNode) -> list[PlanNode]:
    plans = node.get("Plans", [])
    assert isinstance(plans, list)
    return [child for child in plans if isinstance(child, dict)]


def walk(node: PlanNode) -> Iterator[PlanNode]:
    yield node
    for child in children(node):
        yield from walk(child)


def tables(node: PlanNode) -> set[str]:
    return {str(n["Relation Name"]) for n in walk(node) if "Relation Name" in n}


def view_inside_loops(plan: PlanNode) -> list[PlanNode]:
    """Inner sides of the Nested Loops that take plays from outside and run the view inside."""
    inners: list[PlanNode] = []
    for node in walk(plan):
        if node["Node Type"] != "Nested Loop":
            continue
        side = {child.get("Parent Relationship"): child for child in children(node)}
        outer, inner = side.get("Outer"), side.get("Inner")
        if outer is None or inner is None:
            continue
        if PLAYS in tables(outer) and VIEW_TABLES.issubset(tables(inner)):
            inners.append(inner)
    return inners


def looks_up_each_play(inner: PlanNode) -> bool:
    """Every ``play_events`` scan here is keyed on ``id``: one play per loop, not all plays."""
    scans = [n for n in walk(inner) if n.get("Relation Name") == PLAYS]
    keyed = [n for n in scans if any(BY_ID.search(str(n.get(key, ""))) for key in CONDITIONS)]
    return bool(scans) and keyed == scans


def outline(node: PlanNode, depth: int = 0) -> str:
    """The plan as indented lines, for the failure message."""
    conditions = [f"{key}: {node[key]}" for key in (*CONDITIONS, "Hash Cond") if key in node]
    line = " ".join(
        str(part)
        for part in (
            "  " * depth + str(node["Node Type"]),
            node.get("Join Type", ""),
            node.get("Relation Name", ""),
            *conditions,
        )
        if part
    )
    return "\n".join([line, *(outline(child, depth + 1) for child in children(node))])


@pytest.mark.parametrize("settings", PLANNER_SETTINGS.values(), ids=PLANNER_SETTINGS.keys())
def test_reader_resolves_each_play_through_the_fenced_lateral(
    conn: Conn, settings: dict[str, str]
) -> None:
    statements = reader_statements(conn)
    for name, value in settings.items():
        conn.execute("SELECT set_config(%s, %s, true)", (name, value))
        shown = conn.execute("SELECT current_setting(%s) AS v", (name,)).fetchone()
        assert shown is not None and shown["v"] == value, f"{name} was not applied"

    plans = [plan_of(conn, query, params) for query, params in statements]
    resolving = [plan for plan in plans if VIEW_TABLES.issubset(tables(plan))]

    assert resolving, "the reader no longer reads play_file_resolution"
    for plan in resolving:
        assert any(looks_up_each_play(inner) for inner in view_inside_loops(plan)), (
            "play_file_resolution is not evaluated per play:\n" + outline(plan)
        )
