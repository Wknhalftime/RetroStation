"""No write route in ``routers/library/works.py`` runs SQL (artist model phase 0c, SPEC S5).

An AST check over the module. A write route is a function decorated ``@router.post``,
``.put``, ``.patch`` or ``.delete`` (or ``@router.api_route`` with one of those methods).
Neither it nor anything of the module it reaches holds an ``.execute`` or ``.executemany``
call, or names anything imported from psycopg or ``backend.db`` (an async connection, a Pg
adapter). Reached means named anywhere in it, decorator and annotations included, followed
through module-level functions, classes and assignments (a helper passed to
``run_in_threadpool``, a ``partial``, a type alias). The writes run in
``services/work_curation_service.py`` (S3). The read routes (``@router.get``) keep their SQL
until phase 3b rebuilds the Library pages.
"""

from __future__ import annotations

import ast
from pathlib import Path

WORKS_ROUTER = Path(__file__).resolve().parents[1] / "backend" / "routers" / "library" / "works.py"
WRITE_METHODS = {"post", "put", "patch", "delete"}
SQL_CALLS = {"execute", "executemany"}
DB_MODULES = ("psycopg", "psycopg_pool", "backend.db")

TODAYS_WRITE_ROUTES = {
    ("put", "/works/{work_id}/master"),
    ("delete", "/works/{work_id}/master"),
    ("post", "/works/{work_id}/format-overrides"),
    ("delete", "/works/{work_id}/format-overrides/{override_id}"),
    ("post", "/works/{target_id}/merge"),
    ("post", "/works/{work_id}/split"),
    ("patch", "/files/{file_id}/work"),
}


def _constant_strings(node: ast.expr | None) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        return [s for element in node.elts for s in _constant_strings(element)]
    return []


def _routes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[tuple[str, str]]:
    """(method, path) of each ``@router.<method>(...)`` or ``@router.api_route(...)``."""
    found: set[tuple[str, str]] = set()
    for decorator in function.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and isinstance(decorator.func.value, ast.Name)
            and decorator.func.value.id == "router"
        ):
            continue
        keywords = {k.arg: k.value for k in decorator.keywords if k.arg is not None}
        path = _constant_strings(decorator.args[0] if decorator.args else keywords.get("path"))
        if decorator.func.attr == "api_route":
            methods = [m.lower() for m in _constant_strings(keywords.get("methods"))]
        else:
            methods = [decorator.func.attr]
        found |= {(method, p) for method in methods for p in path}
    return found


def _module_names(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, str]]:
    """The module's own definitions by name, and each imported name's module."""
    defined: dict[str, ast.AST] = {}
    imported: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined[node.name] = node
        elif isinstance(node, ast.Assign):
            defined |= {t.id: node for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined[node.target.id] = node
        elif isinstance(node, ast.TypeAlias):
            defined[node.name.id] = node
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name: node.module or "" for a in node.names}
        elif isinstance(node, ast.Import):
            imported |= {a.asname or a.name.split(".")[0]: a.name for a in node.names}
    return defined, imported


def _is_db(module: str) -> bool:
    return any(module == m or module.startswith(f"{m}.") for m in DB_MODULES)


def _reached(
    name: str,
    node: ast.AST,
    module: tuple[dict[str, ast.AST], dict[str, str]],
    seen: set[str],
) -> set[str]:
    """What *node* (the module's *name*) reaches: ``name:line`` of each SQL call, and
    ``module.name`` of each name imported from a database module."""
    defined, imported = module
    found: set[str] = set()
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in SQL_CALLS
        ):
            found.add(f"{name}:{child.lineno}")
        elif isinstance(child, ast.Name) and child.id in imported:
            if _is_db(imported[child.id]):
                found.add(f"{imported[child.id]}.{child.id}")
        elif isinstance(child, ast.Name) and child.id in defined and child.id not in seen:
            seen.add(child.id)
            found |= _reached(child.id, defined[child.id], module, seen)
    return found


def write_route_sql(source: str) -> dict[tuple[str, str], list[str]]:
    """Each write route, as (method, path), and the SQL it reaches."""
    tree = ast.parse(source)
    module = _module_names(tree)
    routes: dict[tuple[str, str], list[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for route in _routes(node):
            if route[0] in WRITE_METHODS:
                routes[route] = sorted(_reached(node.name, node, module, {node.name}))
    return routes


def test_no_write_route_runs_sql() -> None:
    routes = write_route_sql(WORKS_ROUTER.read_text(encoding="utf-8"))

    assert sorted(TODAYS_WRITE_ROUTES - set(routes)) == []
    assert {route: calls for route, calls in routes.items() if calls} == {}


_SAMPLE = """
from functools import partial

from psycopg import AsyncConnection

from backend.dependencies import SyncRepos

Conn = Annotated[AsyncConnection, Depends(get_db_connection)]


@router.get("/works/{work_id}")
async def read(conn: Conn):
    await conn.execute("SELECT 1")

@router.post("/works/{work_id}/merge", response_model=Merged)
async def merge(repos: SyncRepos):
    await _helper(repos)

async def _helper(repos):
    await _deeper(repos)

async def _deeper(repos):
    await repos.cursor().execute("UPDATE works SET title = 'x'")

@router.delete("/works/{work_id}/master", status_code=204)
def forget(repos: SyncRepos):
    run_in_threadpool(_quietly, repos)

def _quietly(repos):
    repos.cursor().executemany("DELETE FROM song_masters", [])

@router.put(path="/works/{work_id}/master")
def choose(repos: SyncRepos):
    return service.choose(repos)

@router.patch("/files/{file_id}/work")
def through_a_class(repos: SyncRepos):
    return _Writes(repos).run()

class _Writes:
    def run(self):
        self.repos.cursor().execute("UPDATE library_files SET work_id = NULL")

_split = partial(_quietly, None)

@router.api_route("/works/{work_id}/split", methods=["POST"])
def through_a_partial():
    _split()

@router.post("/works/{work_id}/format-overrides")
async def on_the_async_connection(conn: Conn):
    return None

def _unused(conn):
    conn.execute("SELECT 1")
"""


def test_the_check_follows_what_a_write_route_reaches_and_skips_reads() -> None:
    reached = {
        route: sorted({call.split(":")[0] for call in calls})
        for route, calls in write_route_sql(_SAMPLE).items()
    }

    assert reached == {
        ("post", "/works/{work_id}/merge"): ["_deeper"],
        ("delete", "/works/{work_id}/master"): ["_quietly"],
        ("put", "/works/{work_id}/master"): [],
        ("patch", "/files/{file_id}/work"): ["_Writes"],
        ("post", "/works/{work_id}/split"): ["_quietly"],
        ("post", "/works/{work_id}/format-overrides"): ["psycopg.AsyncConnection"],
    }
