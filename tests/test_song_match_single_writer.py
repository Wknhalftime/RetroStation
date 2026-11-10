"""One writer for song matches (artist model phase 3a PR 1, SPEC S4; C52, D2, AUD-R040).

``SongMatchService`` (``backend/services/song_match_service.py``, sync, over ports) is the only
code that writes ``matches``: every match decision and every write call on the matches port
goes through it (D2). Phase 4.3 keeps this file unchanged (its S5), so the writers 3a adds
later (E3's release, S9's Apply, ``scripts/apply_credit_picks.py``) call the service too.

- **The SQL.** Every string literal of every ``.py`` file in ``backend/`` and ``scripts/`` (an
  f-string as its literal parts joined; a bare string statement, such as a docstring, is
  skipped), and every ``.sql`` file there, is read as SQL; the migrations and the rollback
  scripts are skipped. SQL that inserts into, updates, deletes from, merges into, truncates,
  copies into, alters or drops ``matches`` writes it. Its owner is the function or method
  holding it; a module or class constant belongs to each function that names it, and a
  constant no function names is its own owner. Only these may own one: the port's adapter,
  ``backend/db/repositories/matches.py``, which only the service calls, and the named
  exceptions (SPEC Context), which keep their locked tests and fakes: the two cascade CTEs of
  ``broadcast_track_identities``, ``artist_cascade.delete_matches_of_lines``,
  ``artist_resolution.delete_pending_song_matches`` and the fold's re-point in
  ``library_files.merge_into``. An exception's SQL stays inside its method. A later phase may
  retire an exception; none may be added. ``scripts/probe_truncate_cost.py``, which empties
  every table of a test database to time TRUNCATE, is test tooling and is allowed by name.
- **The port's writes** are the adapter's public methods whose SQL writes ``matches``: their
  own, a constant's they name, or that of another function of the module they call (today
  ``create``, ``move_to_work``, ``delete_for_identity`` and ``delete_for_file``). Only the
  service calls one on the port, that is on a receiver that names it: its last name, with
  underscores dropped and in any case, is ``matches``, ``matchrepo`` or ``matchrepository``
  (``repos.match_repo``, ``repos.matches``, ``self._matches``), or it builds a
  ``...MatchRepository``. A receiver naming the service (``repos.song_matches``,
  ``SongMatchService(repos.match_repo)``) is the service, not the port. Reads
  (``get_by_identity``) and phase 2b's version-state copies, which write
  ``version_entry_states``, are not match writes.
- **The service** is the class ``SongMatchService``, with no coroutine method (it writes in the
  caller's transaction, C75); its module imports nothing from psycopg or ``backend.db``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from collections.abc import Iterator
from pathlib import Path

from backend.repositories.matches import MatchRepository

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("backend", "scripts")
ADAPTER = "backend/db/repositories/matches.py"
SERVICE = "backend/services/song_match_service.py"
SERVICE_MODULE = "backend.services.song_match_service"
SKIPPED_DIRS = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
DB_MODULES = ("psycopg", "psycopg_pool", "backend.db")

# (file, owner): the SPEC's named exceptions (Context; D2), which stay in their repositories.
NAMED_EXCEPTIONS = {
    (
        "backend/db/repositories/broadcast_track_identities.py",
        "PgBroadcastTrackIdentityRepository.bulk_reject_by_artist",
    ),
    (
        "backend/db/repositories/broadcast_track_identities.py",
        "PgBroadcastTrackIdentityRepository.bulk_defer_by_artist",
    ),
    (
        "backend/db/repositories/artist_cascade.py",
        "PgArtistCascadeRepository.delete_matches_of_lines",
    ),
    (
        "backend/db/repositories/artist_resolution.py",
        "PgArtistResolutionRepository.delete_pending_song_matches",
    ),
    ("backend/db/repositories/library_files.py", "PgLibraryFileRepository.merge_into"),
}
# Test-database tooling, not a match writer: the probe empties every table of a
# ``retrostation_test`` database (it refuses any other) to time TRUNCATE.
TOOLING = {("scripts/probe_truncate_cost.py", "probe_truncate")}

_TABLE = r"(?:ONLY\s+)?(?:public\.)?\"?matches\"?(?![\w.])"
_WRITE = (
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|MERGE\s+INTO|COPY"
    r"|ALTER\s+TABLE(?:\s+IF\s+EXISTS)?|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)\s+"
)
_TRUNCATE = r"\bTRUNCATE(?:\s+TABLE)?\s+(?:[\w.\"]+\s*,\s*)*"  # matches may come later in a list
_WRITES_MATCHES = re.compile(f"{_WRITE}{_TABLE}|{_TRUNCATE}{_TABLE}", re.IGNORECASE)
_PORT_NAMES = {"matches", "matchrepo", "matchrepository"}


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


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def writes_matches(sql: str) -> bool:
    """Whether SQL *sql* writes ``matches``."""
    return _WRITES_MATCHES.search(" ".join(sql.split())) is not None


def _strings(node: ast.AST) -> Iterator[str]:
    """Every string literal under *node*; an f-string as its literal parts joined, read once;
    a bare string statement (a docstring) is skipped."""
    nodes = list(ast.walk(node))
    skipped = {
        id(n.value)
        for n in nodes
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant | ast.JoinedStr)
    }
    parts = {id(part) for n in nodes if isinstance(n, ast.JoinedStr) for part in n.values}
    for n in nodes:
        if id(n) in skipped or id(n) in parts:
            continue
        if isinstance(n, ast.JoinedStr):
            yield "".join(
                part.value
                for part in n.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
        elif isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n.value


def _assigned(statement: ast.stmt) -> list[str]:
    if isinstance(statement, ast.Assign):
        return [t.id for t in statement.targets if isinstance(t, ast.Name)]
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return [statement.target.id]
    return []


class _Unit:
    """A function, a method, a module or class constant, or other code outside any function."""

    def __init__(self, name: str, kind: str, node: ast.AST, owner_class: str | None) -> None:
        self.name, self.kind, self.node, self.owner_class = name, kind, node, owner_class
        self.direct = any(writes_matches(s) for s in _strings(node))
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        members = {
            n.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name)
            and n.value.id in {"self", "cls", owner_class}
        }
        self.uses = names | (
            {f"{owner_class}.{m}" for m in members} if owner_class is not None else set()
        )


def _units(tree: ast.Module) -> dict[str, _Unit]:
    units: dict[str, _Unit] = {}
    loose: list[ast.stmt] = []
    for statement in tree.body:
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            units[statement.name] = _Unit(statement.name, "function", statement, None)
        elif isinstance(statement, ast.ClassDef):
            cls = statement.name
            for item in statement.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    units[f"{cls}.{item.name}"] = _Unit(f"{cls}.{item.name}", "method", item, cls)
                elif names := _assigned(item):
                    for name in names:
                        units[f"{cls}.{name}"] = _Unit(f"{cls}.{name}", "constant", item, cls)
                elif not isinstance(item, ast.Expr):
                    units.setdefault(cls, _Unit(cls, "other", ast.Module([], []), None))
                    units[cls].direct |= any(writes_matches(s) for s in _strings(item))
        elif names := _assigned(statement):
            for name in names:
                units[name] = _Unit(name, "constant", statement, None)
        else:
            loose.append(statement)
    if loose:
        units["<module>"] = _Unit("<module>", "other", ast.Module(loose, []), None)
    return units


def _closure(units: dict[str, _Unit], start: _Unit, kinds: set[str]) -> set[str]:
    """The units of *kinds* that *start* reaches through the names it uses."""
    seen: set[str] = set()
    todo = [u for u in start.uses if u in units and units[u].kind in kinds]
    while todo:
        name = todo.pop()
        if name not in seen:
            seen.add(name)
            todo += [u for u in units[name].uses if u in units and units[u].kind in kinds]
    return seen


def sql_owners(source: str) -> set[str]:
    """The owners in *source* of SQL that writes ``matches`` (``f``, ``Class.method``, a
    constant no function names, ``Class`` or ``<module>`` for other code)."""
    units = _units(ast.parse(source))
    functions = [u for u in units.values() if u.kind in {"function", "method"}]
    named = {c for f in functions for c in _closure(units, f, {"constant"})}
    owners = {
        f.name
        for f in functions
        if f.direct or any(units[c].direct for c in _closure(units, f, {"constant"}))
    }
    for unit in units.values():
        if unit.kind == "constant" and unit.name not in named:
            reached = _closure(units, unit, {"constant"})
            if unit.direct or any(units[c].direct for c in reached):
                owners.add(unit.name)
        elif unit.kind == "other" and unit.direct:
            owners.add(unit.name)
    return owners


def port_write_methods(source: str) -> set[str]:
    """The public methods of *source*'s classes whose SQL writes ``matches``: their own, a
    constant's they name, or that of another function or method of the module they call."""
    units = _units(ast.parse(source))
    owners = sql_owners(source)
    kinds = {"function", "method"}
    return {
        unit.name.split(".", 1)[1]
        for unit in units.values()
        if unit.kind == "method"
        and not unit.name.split(".", 1)[1].startswith("_")
        and ({unit.name} | _closure(units, unit, kinds)) & owners
    }


def names_the_port(receiver: ast.expr) -> bool:
    """Whether *receiver* names the matches port, not the service (module docstring)."""
    if isinstance(receiver, ast.Call):
        return ast.unparse(receiver.func).endswith("MatchRepository")
    if isinstance(receiver, ast.Attribute):
        last = receiver.attr
    elif isinstance(receiver, ast.Name):
        last = receiver.id
    else:
        return False
    return last.replace("_", "").lower() in _PORT_NAMES


def port_calls(source: str, methods: set[str]) -> list[str]:
    """The calls in *source* of *methods* on a receiver that names the port, as
    ``<receiver>.<method>``."""
    return sorted(
        ast.unparse(node.func)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in methods
        and names_the_port(node.func.value)
    )


def match_writers() -> set[tuple[str, str]]:
    """(file, owner) of every piece of scanned code holding SQL that writes ``matches``."""
    found = {
        (relative, owner)
        for relative, path in _files(".py")
        for owner in sql_owners(path.read_text(encoding="utf-8"))
    }
    found |= {
        (relative, "<file>")
        for relative, path in _files(".sql")
        if writes_matches(path.read_text(encoding="utf-8"))
    }
    return found


def port_callers() -> dict[str, list[str]]:
    """Each scanned module calling one of the port's writes on the port, with the calls."""
    methods = port_write_methods(_read(ADAPTER))
    callers = {
        relative: port_calls(path.read_text(encoding="utf-8"), methods)
        for relative, path in _files(".py")
    }
    return {relative: calls for relative, calls in callers.items() if calls}


def _imports(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def _is_db(module: str) -> bool:
    return any(module == m or module.startswith(f"{m}.") for m in DB_MODULES)


# --- the scans -----------------------------------------------------------------------------------


def test_only_the_adapter_and_the_named_exceptions_write_matches() -> None:
    found = match_writers()

    others = sorted(
        f"{relative}::{owner}"
        for relative, owner in found
        if relative != ADAPTER and (relative, owner) not in NAMED_EXCEPTIONS | TOOLING
    )
    assert others == []
    assert any(relative == ADAPTER for relative, _ in found)


def test_the_ports_writes_are_read_from_its_adapter() -> None:
    methods = port_write_methods(_read(ADAPTER))

    assert methods
    assert methods <= MatchRepository.__abstractmethods__
    assert methods.isdisjoint({"get_by_identity", "copy_version_states", "see_version_states"})


def test_only_the_service_calls_the_ports_writes() -> None:
    callers = port_callers()

    assert {relative: calls for relative, calls in callers.items() if relative != SERVICE} == {}
    assert SERVICE in callers


def test_the_service_is_one_sync_class_over_ports() -> None:
    service = importlib.import_module(SERVICE_MODULE).SongMatchService

    assert inspect.isclass(service)
    assert [
        name
        for name, member in inspect.getmembers(service)
        if inspect.iscoroutinefunction(member) or inspect.isasyncgenfunction(member)
    ] == []
    assert sorted(m for m in _imports(_read(SERVICE)) if _is_db(m)) == []


# --- the scans see what they look for ------------------------------------------------------------


def test_the_scan_reads_writes_and_reads() -> None:
    writes = [
        "INSERT INTO matches (id) VALUES (%s)",
        "insert into   public.matches\n (id) values (1)",
        'UPDATE "matches" SET work_id = %s',
        "UPDATE matches m SET work_id = %s WHERE m.work_id = %s",
        "UPDATE ONLY matches SET work_id = NULL",
        "DELETE FROM matches WHERE identity_id = %s",
        "WITH gone AS (DELETE FROM matches WHERE x RETURNING identity_id) SELECT 1",
        "TRUNCATE matches",
        "TRUNCATE TABLE play_events, matches CASCADE",
        "MERGE INTO matches m USING x ON true WHEN MATCHED THEN DELETE",
        "ALTER TABLE matches ADD COLUMN x int",
        "DROP TABLE IF EXISTS matches",
        "COPY matches FROM STDIN",
    ]
    reads = [
        "SELECT * FROM matches WHERE identity_id = %s",
        "UPDATE track_identities t SET match_status = %s FROM matches m WHERE m.identity_id = t.id",
        "DELETE FROM matches_archive",
        "DELETE FROM matches.archive",
        "INSERT INTO version_entry_states SELECT 1 FROM matches",
        "UPDATE format_overrides SET work_id = %s",
        "TRUNCATE play_events, matches_archive",
        "SELECT id FROM track_identities WHERE id = %s FOR UPDATE",
    ]

    assert [w for w in writes if not writes_matches(w)] == []
    assert [r for r in reads if writes_matches(r)] == []


_SAMPLE_MODULE = '''
_DROP = "DELETE FROM matches WHERE id = %s"
_READ = "SELECT 1 FROM matches"
_UNUSED = f"UPDATE matches SET work_id = {1}"
_OUTER = f"WITH x AS ({_DROP}) SELECT 1"


def helper(conn):
    """Never UPDATE matches here: a docstring is skipped."""
    conn.execute(_OUTER)


def reader(conn):
    conn.execute(_READ)


class Repo:
    _MOVE = "UPDATE matches SET work_id = %s"

    def move(self):
        self._conn.execute(self._MOVE)

    def _insert(self):
        self._conn.execute(f"INSERT INTO matches (id) VALUES ({1})")

    def create(self):
        self._insert()

    def drop(self, conn):
        helper(conn)

    def read(self):
        return self._conn.execute("SELECT * FROM matches")
'''


def test_the_scan_finds_each_owner() -> None:
    assert sql_owners(_SAMPLE_MODULE) == {"helper", "_UNUSED", "Repo.move", "Repo._insert"}
    assert port_write_methods(_SAMPLE_MODULE) == {"move", "create", "drop"}
    loose = 'conn.execute("DELETE FROM matches")\n'
    assert sql_owners(loose) == {"<module>"}
    assert sql_owners('"""DELETE FROM matches: a module docstring."""\n') == set()


def test_the_scan_tells_the_port_from_the_service() -> None:
    methods = {"create", "delete_for_identity", "delete_for_file", "move_to_work"}
    on_the_port = (
        "repos.match_repo.create(m)\n"
        "matches.delete_for_file(f)\n"
        "self._matches.move_to_work(f, w)\n"
        "PgMatchRepository(conn).create(m)\n"
        "RepositoryFactory(conn).matches.delete_for_identity(i)\n"
        "repos.MatchRepository.create(m)\n"
    )
    elsewhere = (
        "repos.song_matches.create(m)\n"
        "SongMatchService(repos.match_repo).delete_for_file(f)\n"
        "repos.format_overrides.move_to_work(f, a, b)\n"
        "repos.song_masters.delete_for_file(f)\n"
        "repos.matching_run.create(r)\n"
        "repos.matches.get_by_identity(i)\n"
        "self.create(m)\n"
    )

    assert port_calls(on_the_port, methods) == sorted(
        [
            "repos.match_repo.create",
            "matches.delete_for_file",
            "self._matches.move_to_work",
            "PgMatchRepository(conn).create",
            "RepositoryFactory(conn).matches.delete_for_identity",
            "repos.MatchRepository.create",
        ]
    )
    assert port_calls(elsewhere, methods) == []
