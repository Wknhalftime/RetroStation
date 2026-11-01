"""One writer for ``log_name_links`` (artist model phase 1a, SPEC S5; C30, AUD-R035).

- ``LogNameLinkRepository`` (``backend/repositories/log_name_links.py``) is an ABC whose
  abstract methods are the five the SPEC lists: list by name, list by artist, upsert one, set
  a state, delete a name's links. Its rule-enforcing fake, ``tests/fakes/log_name_links.py``,
  implements it (locked with the 1a tests).
- Only its PostgreSQL adapter, ``backend/db/repositories/log_name_links.py``, holds SQL that
  writes the table (INSERT, UPDATE, DELETE, MERGE, TRUNCATE, COPY, ALTER or DROP TABLE). The
  scan reads every string literal of every ``.py`` file in ``backend/`` and ``scripts/``, and
  every ``.sql`` file there; it skips the migrations and the rollback scripts, which create
  and drop the table.
- ``LogNameLinkService`` (``backend/services/log_name_link_service.py``) is the port's only
  caller: no other module in ``backend/`` or ``scripts/`` calls one of the port's methods
  on a receiver that names the links.
  Wiring code may build the adapter and hand it on; only the service calls it.
"""

from __future__ import annotations

import ast
import inspect
import re
from collections.abc import Iterator
from pathlib import Path

from backend.repositories.log_name_links import LogNameLinkRepository
from tests.fakes.log_name_links import FakeLogNameLinkRepository

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("backend", "scripts")
ADAPTER = "backend/db/repositories/log_name_links.py"
SERVICE = "backend/services/log_name_link_service.py"
PORT_METHODS = {
    "list_by_name",
    "list_by_artist",
    "upsert_link",
    "set_link_state",
    "delete_links_of_name",
}
SKIPPED_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

_WRITES_THE_TABLE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|TRUNCATE(?:\s+TABLE)?|COPY"
    r"|ALTER\s+TABLE(?:\s+IF\s+EXISTS)?|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)"
    r"\s+(?:ONLY\s+)?(?:public\.)?\"?log_name_links\b",
    re.IGNORECASE,
)


def _skipped(relative: str) -> bool:
    return (
        relative.startswith("backend/db/migrations/")
        or re.fullmatch(r"backend/db/rollback_[^/]+\.sql", relative) is not None
    )


def _files(suffix: str) -> Iterator[tuple[str, Path]]:
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob(f"*{suffix}")):
            if SKIPPED_DIRS & set(path.parts):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if not _skipped(relative):
                yield relative, path


def _string_literals(source: str) -> Iterator[str]:
    """Every string literal; an f-string as its literal parts joined, read once."""
    tree = ast.parse(source)
    parts = {
        id(part)
        for node in ast.walk(tree)
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            yield "".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in parts
        ):
            yield node.value


def writes_the_table(text: str) -> bool:
    """Whether SQL *text* writes ``log_name_links``."""
    return _WRITES_THE_TABLE.search(" ".join(text.split())) is not None


def table_writers() -> set[str]:
    """Every scanned file holding SQL that writes the table."""
    found = {
        relative
        for relative, path in _files(".py")
        if any(writes_the_table(s) for s in _string_literals(path.read_text(encoding="utf-8")))
    }
    found |= {
        relative
        for relative, path in _files(".sql")
        if writes_the_table(path.read_text(encoding="utf-8"))
    }
    return found


def port_calls(source: str) -> list[str]:
    """The port methods *source* calls, as ``<receiver>.<method>``: a call of one of the port's
    method names on a receiver that names the links (``self._links``, ``repos.link_repo``,
    ``log_name_links``), so another repository's ``list_by_name`` is not one."""
    return [
        ast.unparse(node.func)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in PORT_METHODS
        and "link" in ast.unparse(node.func.value).lower()
    ]


def port_callers() -> dict[str, list[str]]:
    callers = {
        relative: port_calls(path.read_text(encoding="utf-8")) for relative, path in _files(".py")
    }
    return {relative: calls for relative, calls in callers.items() if calls}


# --- the port and its fake -----------------------------------------------------------------------


def test_the_port_is_an_abc_with_the_five_methods() -> None:
    assert inspect.isabstract(LogNameLinkRepository)
    assert LogNameLinkRepository.__abstractmethods__ == PORT_METHODS
    assert isinstance(FakeLogNameLinkRepository(), LogNameLinkRepository)


# --- the scans -----------------------------------------------------------------------------------


def test_no_other_module_writes_the_table() -> None:
    assert table_writers() == {ADAPTER}


def test_only_the_service_calls_the_port() -> None:
    callers = port_callers()

    assert set(callers) == {SERVICE}
    called = {call.rsplit(".", 1)[1] for call in callers[SERVICE]}
    assert {"upsert_link", "delete_links_of_name", "list_by_name", "list_by_artist"} <= called


def test_the_scans_see_writes_and_calls() -> None:
    writes = [
        "INSERT INTO log_name_links (id) VALUES (%s)",
        "insert into   public.log_name_links\n (id) values (1)",
        'UPDATE "log_name_links" SET state = %s',
        "DELETE FROM log_name_links WHERE broadcast_artist_id = %s",
        "TRUNCATE log_name_links",
        "ALTER TABLE log_name_links ADD COLUMN x int",
        "DROP TABLE IF EXISTS log_name_links",
        "WITH x AS (SELECT 1) DELETE FROM log_name_links USING x",
    ]
    reads = [
        "SELECT * FROM log_name_links WHERE artist_id = %s",
        "SELECT 1 FROM log_name_links l JOIN artists a ON a.id = l.artist_id",
        "UPDATE matches SET work_id = %s",
        "DELETE FROM log_name_links_archive",
    ]

    assert [w for w in writes if not writes_the_table(w)] == []
    assert [r for r in reads if writes_the_table(r)] == []
    assert port_calls("repos.link_repo.upsert_link(link)\nself._links.list_by_name(x)\n") == [
        "repos.link_repo.upsert_link",
        "self._links.list_by_name",
    ]
    assert port_calls("links = PgLogNameLinkRepository(conn)\nrepos.matches.create(m)\n") == []
    assert port_calls("repos.works.list_by_artist(a)\ncredits.list_by_name(n)\n") == []
    f_string = 'f"DELETE FROM log_name_links WHERE id IN ({marks})"'
    assert [writes_the_table(s) for s in _string_literals(f_string)] == [True]
