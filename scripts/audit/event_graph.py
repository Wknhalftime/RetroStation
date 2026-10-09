"""Event-graph analyzer: how the parts of the code base hand work to each other.

Parses every ``.py`` file under ``--root`` (never imports or runs it) and writes one JSON graph:
Huey task hand-offs (ENQUEUE, ENQUEUE_DEFERRED, INLINE), publish/subscribe (PUBLISH, SUBSCRIBE)
and polling loops (POLL); each enqueue classed COMMAND or QUERY and each publish EVENT; every
dispatch the AST cannot resolve; and the rule hits EV01-EV10. The output contract is
audit/eda/contract.md.

Usage:
    uv run python scripts/audit/event_graph.py --ev01-scope library_scan_task --summary
    uv run python scripts/audit/event_graph.py --check audit/event-graph.baseline.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1
EXCLUDED_DIRS = frozenset({"__pycache__", "frontend", "htmlcov", "node_modules", "tests"})
DEFAULT_GUARD = "backend.tasks._enqueue_chain:enqueue_or_log"
EDGE_KINDS = ("ENQUEUE", "ENQUEUE_DEFERRED", "INLINE", "PUBLISH", "SUBSCRIBE", "POLL")
ENQUEUE_KINDS = frozenset({"ENQUEUE", "ENQUEUE_DEFERRED"})
TASK_EDGE_KINDS = ENQUEUE_KINDS | {"INLINE"}
CLASSES = ("COMMAND", "QUERY", "EVENT")
RULES = tuple(f"EV{n:02d}" for n in range(1, 11))
INLINE_ATTRS = frozenset({"call_local", "func"})
QUERY_ATTRS = frozenset({"execute", "executemany", "fetchone", "fetchall", "fetchmany"})
LAYERS = {"routers": "router", "tasks": "task", "services": "service", "scripts": "script"}
LOW_LAYERS = frozenset({"services", "db", "repositories", "domain", "playout"})
PLAIN_TYPES = frozenset({"str", "int", "float", "bool", "None"})
NOTIFY_RES = (
    re.compile(r"\bNOTIFY\s+(\w+)", re.IGNORECASE),
    re.compile(r"pg_notify\(\s*'(\w+)'", re.IGNORECASE),
)
LISTEN_RE = re.compile(r"\bLISTEN\s+(\w+)", re.IGNORECASE)
TABLE_RE = re.compile(r"\b(?:FROM|UPDATE|INTO)\s+(\w+)", re.IGNORECASE)
MODULE_SCOPE = "<module>"
SYMBOL_DEPTH = 20

type DefNode = ast.FunctionDef | ast.AsyncFunctionDef
type Row = dict[str, object]


class AnalysisError(Exception):
    """The tree cannot be analysed; the CLI exits 2."""


@dataclass(frozen=True)
class Target:
    """What a name or attribute refers to.

    kind: module (``module`` is its dotted name), def / class / instance (defined in
    ``module`` as ``name``), symbol (an in-tree ``from module import name``, not yet followed),
    external (``module`` is the dotted path outside the tree), unbound, other.
    """

    kind: str
    module: str = ""
    name: str = ""


OTHER = Target("other")
UNBOUND = Target("unbound")


@dataclass
class Module:
    name: str
    file: str
    source: str
    tree: ast.Module
    is_package: bool
    bindings: dict[str, Target] = field(default_factory=dict)
    parents: dict[ast.AST, ast.AST] = field(default_factory=dict)


@dataclass(frozen=True)
class Instance:
    module: str
    name: str
    file: str
    line: int
    results_false: bool

    @property
    def qualname(self) -> str:
        return f"{self.module}:{self.name}"


@dataclass
class Task:
    name: str
    module: str
    file: str
    node: DefNode
    kind: str
    instance: Instance
    schedule: str | None
    retries: object


@dataclass
class Scope:
    module: Module
    qualname: str
    kind: str
    line: int
    bindings: dict[str, Target]
    parent: Scope | None
    own: list[ast.AST]
    node: DefNode | ast.ClassDef | None = None


@dataclass
class Edge:
    kind: str
    producer: str
    consumer: str
    file: str
    lines: set[int] = field(default_factory=set)
    guarded: bool = True
    query: bool = False

    @property
    def message_class(self) -> str | None:
        if self.kind == "PUBLISH":
            return "EVENT"
        if self.kind in ENQUEUE_KINDS:
            return "QUERY" if self.query else "COMMAND"
        return None


@dataclass(frozen=True)
class Options:
    guard: tuple[str, str]
    publish: frozenset[tuple[str, str]]
    subscribe: frozenset[tuple[str, str]]
    ev01_scope: frozenset[str]


@dataclass
class Graph:
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    unresolved: list[Row] = field(default_factory=list)
    producers: dict[str, tuple[str, int]] = field(default_factory=dict)
    low_refs: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)

    def add_edge(
        self,
        key: tuple[str, str, str],
        file: str,
        line: int,
        *,
        guarded: bool = False,
        query: bool = False,
    ) -> None:
        edge = self.edges.get(key)
        if edge is None:
            edge = self.edges[key] = Edge(*key, file=file)
        edge.lines.add(line)
        edge.guarded = edge.guarded and guarded
        edge.query = edge.query or query

    def add_unresolved(self, scope: Scope, line: int, reason: str, name: str | None) -> None:
        row: Row = {"file": scope.module.file, "line": line, "producer": scope.qualname}
        self.unresolved.append({**row, "reason": reason, "name": name})


# ---- loading ----


def python_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in EXCLUDED_DIRS and not d.startswith(".")
        )
        found.extend(Path(dirpath, f) for f in sorted(filenames) if f.endswith(".py"))
    return found


def load_module(root: Path, path: Path) -> Module:
    rel = path.relative_to(root).as_posix()
    try:
        source = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(source, filename=rel)
    except (OSError, UnicodeDecodeError) as exc:
        raise AnalysisError(f"cannot read {rel}: {exc}") from exc
    except SyntaxError as exc:
        raise AnalysisError(f"cannot parse {rel}: {exc.msg} (line {exc.lineno})") from exc
    parts = rel[: -len(".py")].split("/")
    is_package = len(parts) > 1 and parts[-1] == "__init__"
    name = ".".join(parts[:-1] if is_package else parts)
    module = Module(name, rel, source, tree, is_package)
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            module.parents[child] = node
    return module


def import_base(module: Module, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    parts = module.name.split(".")
    if not module.is_package:
        parts = parts[:-1]
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    return ".".join([*parts, *([node.module] if node.module else [])])


def block_statements(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Statements of a block and of its nested if/try/with/for blocks, not of defs."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.stmt):
                yield from block_statements([child])
            elif isinstance(child, ast.ExceptHandler | ast.match_case):
                yield from block_statements(child.body)


def own_nodes(body: Sequence[ast.AST]) -> tuple[list[ast.AST], list[ast.AST]]:
    """Nodes evaluated in this scope (source order) and the defs/classes nested in it.

    A nested def contributes its decorators and defaults, not its body. A lambda is not a scope.
    """
    nodes: list[ast.AST] = []
    defs: list[ast.AST] = []
    stack = list(reversed(body))
    while stack:
        node = stack.pop()
        nodes.append(node)
        outer: list[ast.AST]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            defs.append(node)
            defaults = [d for d in node.args.kw_defaults if d is not None]
            outer = [*node.decorator_list, *node.args.defaults, *defaults]
        elif isinstance(node, ast.ClassDef):
            defs.append(node)
            outer = [*node.decorator_list, *node.bases, *node.keywords]
        else:
            outer = list(ast.iter_child_nodes(node))
        stack.extend(reversed(outer))
    return nodes, defs


def is_doc_string(node: ast.Constant, parents: dict[ast.AST, ast.AST]) -> bool:
    """A string statement documents; it is never executed as SQL or read as a reference."""
    return isinstance(parents.get(node), ast.Expr)


# ---- resolution ----


class Index:
    """Every parsed module, and the names, Huey instances and tasks they define."""

    def __init__(self, modules: dict[str, Module]) -> None:
        self.modules = modules
        self.packages = {
            ".".join(name.split(".")[:n]) for name in modules for n in range(1, name.count(".") + 2)
        }
        self.instances: dict[tuple[str, str], Instance] = {}
        self.tasks: dict[tuple[str, str], Task] = {}
        self.signals: list[tuple[Module, DefNode, Instance, list[str]]] = []
        self.scopes: dict[str, Scope] = {}

    def is_package(self, name: str) -> bool:
        return name in self.packages

    def module_target(self, name: str) -> Target:
        return Target("module", name) if self.is_package(name) else Target("external", name)

    def import_bindings(self, module: Module, node: ast.AST) -> Iterator[tuple[str, Target]]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    yield alias.asname, self.module_target(alias.name)
                else:
                    top = alias.name.split(".")[0]
                    yield top, self.module_target(top)
        elif isinstance(node, ast.ImportFrom):
            base = import_base(module, node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                sub = f"{base}.{alias.name}"
                if self.is_package(sub):
                    target = Target("module", sub)
                elif self.is_package(base):
                    target = Target("symbol", base, alias.name)
                else:
                    target = Target("external", sub)
                yield alias.asname or alias.name, target

    def lookup(self, module: str, name: str, depth: int = 0) -> Target:
        found = self.modules.get(module)
        if found is not None and name in found.bindings:
            return self.deref(found.bindings[name], depth + 1)
        sub = f"{module}.{name}"
        if self.is_package(sub):
            return Target("module", sub)
        if not self.is_package(module):
            return Target("external", sub)
        return OTHER

    def deref(self, target: Target, depth: int = 0) -> Target:
        if target.kind != "symbol":
            return target
        if depth > SYMBOL_DEPTH:
            return OTHER
        return self.lookup(target.module, target.name, depth)

    def resolve(self, expr: ast.AST, scope: Scope) -> Target:
        if isinstance(expr, ast.Name):
            return self.lookup_name(expr.id, scope)
        if isinstance(expr, ast.Attribute):
            base = self.resolve(expr.value, scope)
            if base.kind == "module":
                return self.lookup(base.module, expr.attr)
            if base.kind == "external":
                return Target("external", f"{base.module}.{expr.attr}")
        return OTHER

    def lookup_name(self, name: str, scope: Scope) -> Target:
        current: Scope | None = scope
        while current is not None:
            visible = current is scope or current.kind != "class"
            if visible and name in current.bindings:
                return self.deref(current.bindings[name])
            current = current.parent
        return UNBOUND

    def task_of(self, target: Target) -> Task | None:
        if target.kind != "def":
            return None
        return self.tasks.get((target.module, target.name))

    def module_scope(self, module: Module) -> Scope:
        return self.scopes[f"{module.name}:{MODULE_SCOPE}"]

    def defines_tasks(self, module: str) -> bool:
        return any(task.module == module for task in self.tasks.values())

    def imports_of(self, module: Module) -> set[str]:
        imported: set[str] = set()
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = import_base(module, node)
                imported.add(base)
                subs = (f"{base}.{alias.name}" for alias in node.names)
                imported.update(sub for sub in subs if self.is_package(sub))
        return imported


def bind_module(index: Index, module: Module) -> None:
    """Module-level names: imports and defs rebind; any other assignment never overrides."""
    nodes, _ = own_nodes(module.tree.body)
    for node in nodes:
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            module.bindings.setdefault(node.id, OTHER)
    for node in nodes:
        module.bindings.update(index.import_bindings(module, node))
    for stmt in block_statements(module.tree.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            module.bindings[stmt.name] = Target("def", module.name, stmt.name)
        elif isinstance(stmt, ast.ClassDef):
            module.bindings[stmt.name] = Target("class", module.name, stmt.name)


def build_scopes(index: Index, module: Module) -> None:
    nodes, defs = own_nodes(module.tree.body)
    root = Scope(module, f"{module.name}:{MODULE_SCOPE}", "module", 1, module.bindings, None, nodes)
    index.scopes[root.qualname] = root
    pending = [(root, d) for d in defs]
    while pending:
        parent, node = pending.pop()
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        prefix = "" if parent.kind == "module" else parent.qualname.partition(":")[2] + "."
        qual = prefix + node.name
        body, nested = own_nodes(node.body)
        bindings: dict[str, Target] = {}
        if not isinstance(node, ast.ClassDef):
            args = node.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
            bindings.update((a.arg, OTHER) for a in params if a is not None)
        for inner in body:
            if isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Store):
                bindings.setdefault(inner.id, OTHER)
        for inner in body:
            bindings.update(index.import_bindings(module, inner))
        for inner in nested:
            assert isinstance(inner, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            kind = "class" if isinstance(inner, ast.ClassDef) else "def"
            bindings[inner.name] = Target(kind, module.name, f"{qual}.{inner.name}")
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        scope = Scope(
            module, f"{module.name}:{qual}", kind, node.lineno, bindings, parent, body, node
        )
        index.scopes[scope.qualname] = scope
        pending.extend((scope, d) for d in nested)


def is_huey_class(target: Target) -> bool:
    return (
        target.kind == "external"
        and target.module.split(".")[0] == "huey"
        and target.module.rsplit(".", 1)[-1].endswith("Huey")
    )


def find_instances(index: Index, module: Module) -> None:
    scope = index.module_scope(module)
    for stmt in block_statements(module.tree.body):
        if not (isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Call)):
            continue
        if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
            continue
        if not is_huey_class(index.resolve(stmt.value.func, scope)):
            continue
        results = [kw.value for kw in stmt.value.keywords if kw.arg == "results"]
        results_false = bool(results) and isinstance(results[0], ast.Constant)
        results_false = results_false and getattr(results[0], "value", None) is False
        name = stmt.targets[0].id
        index.instances[(module.name, name)] = Instance(
            module.name, name, module.file, stmt.lineno, results_false
        )
        module.bindings[name] = Target("instance", module.name, name)


def decorator_role(
    index: Index, scope: Scope, decorator: ast.expr
) -> tuple[str, Instance, ast.Call | None] | None:
    call = decorator if isinstance(decorator, ast.Call) else None
    func = call.func if call is not None else decorator
    if not isinstance(func, ast.Attribute):
        return None
    target = index.resolve(func.value, scope)
    instance = index.instances.get((target.module, target.name))
    if target.kind != "instance" or instance is None:
        return None
    return func.attr, instance, call


def find_tasks(index: Index, module: Module) -> None:
    scope = index.module_scope(module)
    for stmt in block_statements(module.tree.body):
        if not isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in stmt.decorator_list:
            role = decorator_role(index, scope, decorator)
            if role is None:
                continue
            attr, instance, call = role
            if attr in ("task", "periodic_task"):
                index.tasks[(module.name, stmt.name)] = new_task(module, stmt, attr, instance, call)
            elif attr == "signal":
                args = call.args if call is not None else []
                names = [ast.unparse(arg) for arg in args] or ["*"]
                index.signals.append((module, stmt, instance, names))


def new_task(
    module: Module, node: DefNode, attr: str, instance: Instance, call: ast.Call | None
) -> Task:
    schedule = None
    retries: object = 0
    if call is not None:
        if attr == "periodic_task" and call.args:
            schedule = ast.get_source_segment(module.source, call.args[0])
        for kw in call.keywords:
            if kw.arg == "retries":
                value = kw.value
                retries = value.value if isinstance(value, ast.Constant) else ast.unparse(value)
    kind = "periodic" if attr == "periodic_task" else "task"
    return Task(node.name, module.name, module.file, node, kind, instance, schedule, retries)


def build_index(root: Path) -> Index:
    modules: dict[str, Module] = {}
    for path in python_files(root):
        module = load_module(root, path)
        modules[module.name] = module
    index = Index(modules)
    for module in modules.values():
        bind_module(index, module)
    for module in modules.values():
        build_scopes(index, module)
    for module in modules.values():
        find_instances(index, module)
    for module in modules.values():
        find_tasks(index, module)
    return index


# ---- edges ----


def is_low_layer(file: str) -> bool:
    return bool(LOW_LAYERS.intersection(file.split("/")[:-1]))


def call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def task_call(index: Index, scope: Scope, call: ast.Call) -> tuple[str, Task] | None:
    """The edge kind and task a call names directly: T(...), T.schedule(...), T.call_local(...)."""
    task = index.task_of(index.resolve(call.func, scope))
    if task is not None:
        return "ENQUEUE", task
    if isinstance(call.func, ast.Attribute):
        task = index.task_of(index.resolve(call.func.value, scope))
        if task is not None and call.func.attr == "schedule":
            return "ENQUEUE_DEFERRED", task
        if task is not None and call.func.attr in INLINE_ATTRS:
            return "INLINE", task
    return None


def result_is_read(call: ast.Call, scope: Scope) -> bool:
    """T(...).get(, T(...)(, or the name it is bound to later .get(-ed or called."""
    parents = scope.module.parents
    parent = parents.get(call)
    if isinstance(parent, ast.Call) and parent.func is call:
        return True
    if isinstance(parent, ast.Attribute) and parent.attr == "get":
        grand = parents.get(parent)
        if isinstance(grand, ast.Call) and grand.func is parent:
            return True
    names: set[str] = set()
    if isinstance(parent, ast.Assign) and parent.value is call:
        names = {t.id for t in parent.targets if isinstance(t, ast.Name)}
    elif isinstance(parent, ast.AnnAssign | ast.NamedExpr) and parent.value is call:
        names = {parent.target.id} if isinstance(parent.target, ast.Name) else set()
    for node in scope.own:
        if not isinstance(node, ast.Call) or node.lineno < call.lineno:
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            return True
        owner = func.value if isinstance(func, ast.Attribute) and func.attr == "get" else None
        if isinstance(owner, ast.Name) and owner.id in names:
            return True
    return False


class ScopeAnalysis:
    """The edges and unresolved dispatches one scope's own code makes."""

    def __init__(self, index: Index, opts: Options, graph: Graph, scope: Scope) -> None:
        self.index = index
        self.opts = opts
        self.graph = graph
        self.scope = scope
        self.file = scope.module.file
        self.parents = scope.module.parents
        self.consumed: set[ast.AST] = set()
        self.guarded_calls: dict[ast.AST, int] = {}

    def run(self) -> None:
        for node in self.scope.own:
            if isinstance(node, ast.Call):
                self.special_call(node)
            elif isinstance(node, ast.While | ast.For | ast.AsyncFor):
                self.loop(node)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                self.sql_text(node, node.value)
        for node in self.scope.own:
            if node in self.consumed or not isinstance(node, ast.Name | ast.Attribute):
                continue
            if not isinstance(node.ctx, ast.Load):
                continue
            task = self.index.task_of(self.index.resolve(node, self.scope))
            if task is not None:
                self.task_reference(node, task)

    def task_edge(
        self, kind: str, task: Task, line: int, *, guarded: bool = False, query: bool = False
    ) -> None:
        producer = self.scope.qualname
        key = (kind, producer, task.name)
        self.graph.add_edge(key, self.file, line, guarded=guarded, query=query)
        self.graph.producers[producer] = (self.file, self.scope.line)
        if is_low_layer(self.file):
            self.graph.low_refs.setdefault((self.scope.module.name, task.name), (self.file, line))

    def special_call(self, call: ast.Call) -> None:
        target = self.index.resolve(call.func, self.scope)
        if target.kind == "def":
            key = (target.module, target.name)
            if key == self.opts.guard and call.args:
                self.guard(call, call.args[0])
            if key in self.opts.publish:
                self.publish(call)
            if key in self.opts.subscribe:
                self.subscribe(call)
        elif target.kind == "unbound" and call_name(call) == "getattr" and call.args:
            owner = self.index.resolve(call.args[0], self.scope)
            if owner.kind == "module" and self.index.defines_tasks(owner.module):
                self.graph.add_unresolved(self.scope, call.lineno, "getattr", None)

    def guard(self, call: ast.Call, arg: ast.expr) -> None:
        if isinstance(arg, ast.Lambda):
            for inner in ast.walk(arg.body):
                if not isinstance(inner, ast.Call):
                    continue
                found = task_call(self.index, self.scope, inner)
                if found is not None and found[0] in ENQUEUE_KINDS:
                    self.guarded_calls[inner] = call.lineno
            return
        task = self.index.task_of(self.index.resolve(arg, self.scope))
        if task is not None:
            self.consumed.add(arg)
            self.task_edge("ENQUEUE", task, call.lineno, guarded=True)

    def topic(self, call: ast.Call) -> str:
        first = call.args[0] if call.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return f"topic:{first.value}"
        return "topic:?"

    def publish(self, call: ast.Call) -> None:
        producer = self.scope.qualname
        self.graph.add_edge(("PUBLISH", producer, self.topic(call)), self.file, call.lineno)
        self.graph.producers[producer] = (self.file, self.scope.line)

    def subscribe(self, call: ast.Call) -> None:
        handler = call.args[1] if len(call.args) > 1 else None
        target = self.index.resolve(handler, self.scope) if handler is not None else OTHER
        if target.kind not in ("def", "class"):
            self.graph.add_unresolved(self.scope, call.lineno, "subscribe_handler", None)
            return
        consumer = f"{target.module}:{target.name}"
        self.graph.add_edge(("SUBSCRIBE", self.topic(call), consumer), self.file, call.lineno)

    def loop(self, loop: ast.While | ast.For | ast.AsyncFor) -> None:
        body, _ = own_nodes(loop.body)
        calls = [n for n in body if isinstance(n, ast.Call)]
        sleeps = any(call_name(c) == "sleep" for c in calls)
        queries = any(
            isinstance(c.func, ast.Attribute) and c.func.attr in QUERY_ATTRS for c in calls
        )
        if not (sleeps and queries):
            return
        table = "?"
        for node in body:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                match = TABLE_RE.search(node.value)
                if match and not is_doc_string(node, self.parents):
                    table = match.group(1)
                    break
        producer = self.scope.qualname
        self.graph.add_edge(("POLL", producer, f"table:{table}"), self.file, loop.lineno)
        self.graph.producers[producer] = (self.file, self.scope.line)

    def sql_text(self, node: ast.Constant, text: str) -> None:
        if is_doc_string(node, self.parents):
            return
        producer = self.scope.qualname
        for pattern in NOTIFY_RES:
            for match in pattern.finditer(text):
                channel = f"pg-channel:{match.group(1)}"
                self.graph.add_edge(("PUBLISH", producer, channel), self.file, node.lineno)
                self.graph.producers[producer] = (self.file, self.scope.line)
        for match in LISTEN_RE.finditer(text):
            channel = f"pg-channel:{match.group(1)}"
            self.graph.add_edge(("SUBSCRIBE", channel, producer), self.file, node.lineno)

    def task_reference(self, node: ast.Name | ast.Attribute, task: Task) -> None:
        parent = self.parents.get(node)
        call: ast.AST | None = None
        kind = ""
        if isinstance(parent, ast.Call) and parent.func is node:
            call, kind = parent, "ENQUEUE"
        elif isinstance(parent, ast.Attribute) and parent.value is node:
            grand = self.parents.get(parent)
            if isinstance(grand, ast.Call) and grand.func is parent:
                if parent.attr == "schedule":
                    call, kind = grand, "ENQUEUE_DEFERRED"
                elif parent.attr in INLINE_ATTRS:
                    call, kind = grand, "INLINE"
        if isinstance(call, ast.Call) and kind == "INLINE":
            self.task_edge(kind, task, call.lineno)
        elif isinstance(call, ast.Call):
            guard_line = self.guarded_calls.get(call)
            line = call.lineno if guard_line is None else guard_line
            query = result_is_read(call, self.scope)
            self.task_edge(kind, task, line, guarded=guard_line is not None, query=query)
        elif isinstance(parent, ast.Dict | ast.List | ast.Set | ast.Tuple):
            self.graph.add_unresolved(self.scope, node.lineno, "task_table", task.name)
        else:
            self.graph.add_unresolved(self.scope, node.lineno, "task_as_value", task.name)


def signal_edges(index: Index, graph: Graph) -> None:
    for module, handler, instance, names in index.signals:
        consumer = f"{module.name}:{handler.name}"
        line = handler.decorator_list[0].lineno if handler.decorator_list else handler.lineno
        for name in names:
            topic = f"huey-signal:{name}"
            graph.add_edge(("SUBSCRIBE", topic, consumer), module.file, line)
            graph.add_edge(("PUBLISH", instance.qualname, topic), instance.file, instance.line)
            graph.producers[instance.qualname] = (instance.file, instance.line)


def import_refs(index: Index, graph: Graph) -> None:
    """EV03's other half: a low-layer module importing a task by name."""
    for module in index.modules.values():
        if not is_low_layer(module.file):
            continue
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for _, target in index.import_bindings(module, node):
                task = index.task_of(index.deref(target))
                if task is not None:
                    key = (module.name, task.name)
                    graph.low_refs.setdefault(key, (module.file, node.lineno))


def build_graph(index: Index, opts: Options) -> Graph:
    graph = Graph()
    for scope in index.scopes.values():
        ScopeAnalysis(index, opts, graph, scope).run()
    signal_edges(index, graph)
    import_refs(index, graph)
    return graph


# ---- nodes ----


def task_scope(index: Index, task: Task) -> Scope:
    return index.scopes[f"{task.module}:{task.name}"]


def has_handler(scope: Scope) -> bool:
    """The scope's own body holds a try with at least one except handler."""
    return any(isinstance(n, ast.Try | ast.TryStar) and n.handlers for n in scope.own)


def is_catching_context_manager(index: Index, target: Target) -> bool:
    """A ``@contextmanager`` def whose body catches (a try with a handler)."""
    scope = index.scopes.get(f"{target.module}:{target.name}")
    node = scope.node if scope is not None else None
    if scope is None or not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return False
    decorated = any(
        (d.id if isinstance(d, ast.Name) else d.attr if isinstance(d, ast.Attribute) else "")
        == "contextmanager"
        for d in node.decorator_list
    )
    return decorated and has_handler(scope)


def envelope(index: Index, task: Task) -> str:
    scope = task_scope(index, task)
    called: list[Target] = []
    for node in scope.own:
        if isinstance(node, ast.With | ast.AsyncWith):
            for item in node.items:
                if isinstance(item.context_expr, ast.Call):
                    target = index.resolve(item.context_expr.func, scope)
                    if target.kind == "def":
                        called.append(target)
    names = {t.name.rsplit(".", 1)[-1] for t in called}
    for name in ("task_run", "task_failure_telemetry"):
        if name in names:
            return name
    if any(is_catching_context_manager(index, t) for t in called):
        return "context"
    return "own" if has_handler(scope) else "none"


def body_lines(node: DefNode) -> int:
    end = node.end_lineno if node.end_lineno is not None else node.lineno
    return end - node.body[0].lineno + 1


def params(node: DefNode) -> list[Row]:
    args = node.args
    every = [*args.posonlyargs, *args.args, args.vararg, *args.kwonlyargs, args.kwarg]
    return [
        {"name": a.arg, "annotation": ast.unparse(a.annotation) if a.annotation else None}
        for a in every
        if a is not None
    ]


def registered(index: Index, task: Task, imports: dict[str, set[str]]) -> bool:
    app = task.instance.module
    if app not in imports:
        imports[app] = index.imports_of(index.modules[app])
    return task.module in imports[app]


def producer_layer(qualname: str, file: str) -> str:
    if qualname.partition(":")[2].rsplit(".", 1)[-1] == "lifespan":
        return "lifespan"
    for segment in file.split("/")[:-1]:
        if segment in LAYERS:
            return LAYERS[segment]
    return "other"


# ---- rules ----


def producer_task(index: Index, qualname: str) -> Task | None:
    module, _, qual = qualname.partition(":")
    return index.tasks.get((module, qual.split(".")[0]))


def task_graph(index: Index, edges: Iterable[Edge]) -> dict[str, set[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        source = producer_task(index, edge.producer)
        if edge.kind in ENQUEUE_KINDS and source is not None:
            adjacency.setdefault(source.name, set()).add(edge.consumer)
    return adjacency


def cycles(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Elementary cycles, each starting at its smallest name."""
    found: list[list[str]] = []

    def walk(start: str, path: list[str]) -> None:
        for nxt in sorted(adjacency.get(path[-1], ())):
            if nxt == start:
                found.append([*path, start])
            elif nxt > start and nxt not in path:
                walk(start, [*path, nxt])

    for start in sorted(adjacency):
        walk(start, [start])
    return found


def longest_paths(adjacency: dict[str, set[str]]) -> list[list[str]]:
    paths: list[list[str]] = []

    def walk(path: list[str]) -> None:
        nexts = [n for n in sorted(adjacency.get(path[-1], ())) if n not in path]
        if not nexts and len(path) > 1:
            paths.append(path)
        for nxt in nexts:
            walk([*path, nxt])

    for start in sorted(adjacency):
        walk([start])
    longest = max((len(p) for p in paths), default=0)
    return [p for p in paths if len(p) == longest]


def is_plain(annotation: ast.expr, *, containers: bool = True) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id in PLAIN_TYPES
    if isinstance(annotation, ast.Constant):
        return annotation.value is None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left = is_plain(annotation.left, containers=False)
        return left and is_plain(annotation.right, containers=False)
    if not (containers and isinstance(annotation, ast.Subscript)):
        return False
    if not isinstance(annotation.value, ast.Name):
        return False
    inner = annotation.slice
    items = inner.elts if isinstance(inner, ast.Tuple) else [inner]
    arity = {"list": 1, "dict": 2}.get(annotation.value.id)
    return len(items) == arity and all(is_plain(i, containers=False) for i in items)


def reads_state(scope: Scope) -> bool:
    assigned = {n.id for n in scope.own if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    for node in scope.own:
        if not isinstance(node, ast.If | ast.While):
            continue
        for part in ast.walk(node.test):
            if isinstance(part, ast.Call):
                return True
            if isinstance(part, ast.Name) and part.id in assigned:
                return True
    return False


def service_calls(index: Index, scope: Scope) -> int:
    callees: set[tuple[str, str]] = set()
    for node in scope.own:
        if isinstance(node, ast.Call):
            target = index.resolve(node.func, scope)
            if target.kind == "def" and "services" in target.module.split("."):
                callees.add((target.module, target.name))
    return len(callees)


def hit(code: str, cls: str, subject: str, file: str, line: int, **extra: object) -> Row:
    return {"code": code, "class": cls, "subject": subject, "file": file, "line": line, **extra}


def edge_hit(code: str, cls: str, edge: Edge, **extra: object) -> Row:
    subject = f"{edge.producer} -> {edge.consumer}"
    return hit(code, cls, subject, edge.file, min(edge.lines), **extra)


def task_hits(index: Index, graph: Graph, edges: list[Edge], reg: dict[str, bool]) -> list[Row]:
    rows: list[Row] = []
    fed = {e.consumer for e in edges if e.kind in TASK_EDGE_KINDS}
    unresolved_names = {row["name"] for row in graph.unresolved}
    modules: dict[str, Task] = {}
    for task in sorted(index.tasks.values(), key=lambda t: (t.module, t.node.lineno)):
        subject = f"{task.module}:{task.name}"
        if not reg[subject]:
            modules.setdefault(task.module, task)
        if task.kind == "task" and task.name not in fed:
            referenced = task.name in unresolved_names
            rows.append(
                hit(
                    "EV06",
                    "inventory",
                    subject,
                    task.file,
                    task.node.lineno,
                    detail="no_producer",
                    referenced_in_unresolved_dispatch=referenced,
                )
            )
        args = task.node.args
        for param in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if param.annotation is not None and not is_plain(param.annotation):
                rows.append(
                    hit(
                        "EV07",
                        "inventory",
                        f"{subject}({param.arg})",
                        task.file,
                        param.lineno,
                        annotation=ast.unparse(param.annotation),
                    )
                )
        scope = task_scope(index, task)
        value = {"body_lines": body_lines(task.node), "service_calls": service_calls(index, scope)}
        rows.append(hit("EV08", "metric", subject, task.file, task.node.lineno, value=value))
        if task.kind == "periodic" and reads_state(scope):
            rows.append(hit("EV09", "inventory", subject, task.file, task.node.lineno))
    for module, task in modules.items():
        rows.append(
            hit(
                "EV06",
                "inventory",
                module,
                task.file,
                task.node.lineno,
                detail="unregistered_module",
            )
        )
    return rows


def edge_hits(index: Index, graph: Graph, edges: list[Edge], opts: Options) -> list[Row]:
    rows: list[Row] = []
    named: dict[str, set[str]] = {}
    for edge in edges:
        source = producer_task(index, edge.producer)
        if edge.kind in ENQUEUE_KINDS and not edge.guarded and source is not None:
            scope = "stated" if source.name in opts.ev01_scope else "unstated"
            rows.append(edge_hit("EV01", "ruling", edge, ruling_scope=scope))
        if edge.kind == "INLINE":
            rows.append(edge_hit("EV02", "inventory", edge))
        if edge.message_class == "QUERY":
            rows.append(edge_hit("EV05", "inventory", edge))
        if edge.kind == "POLL":
            file, line = graph.producers[edge.producer]
            rows.append(hit("EV09", "inventory", edge.producer, file, line))
        if edge.kind in TASK_EDGE_KINDS:
            named.setdefault(edge.producer, set()).add(edge.consumer)
    for (module, task_name), (file, line) in graph.low_refs.items():
        rows.append(hit("EV03", "inventory", f"{module} -> {task_name}", file, line))
    adjacency = task_graph(index, edges)
    for cycle in cycles(adjacency):
        first = index_task_file(index, cycle[0])
        rows.append(hit("EV04", "inventory", " -> ".join(cycle), *first))
    for instance in index.instances.values():
        if not instance.results_false:
            rows.append(hit("EV05", "inventory", instance.qualname, instance.file, instance.line))
    for producer, consumers in named.items():
        file, line = graph.producers[producer]
        rows.append(hit("EV10", "metric", producer, file, line, value={"tasks": len(consumers)}))
    return rows


def index_task_file(index: Index, name: str) -> tuple[str, int]:
    for task in index.tasks.values():
        if task.name == name:
            return task.file, task.node.lineno
    return "", 0


# ---- output ----


def analyse(root: Path, opts: Options, header: Row) -> tuple[Row, dict[str, set[str]]]:
    """The output document, and the task graph the --chains listing walks."""
    index = build_index(root)
    graph = build_graph(index, opts)
    edges = sorted(graph.edges.values(), key=lambda e: (e.kind, e.producer, e.consumer))
    imports: dict[str, set[str]] = {}
    reg = {f"{t.module}:{t.name}": registered(index, t, imports) for t in index.tasks.values()}
    tasks = sorted(index.tasks.values(), key=lambda t: (t.module, t.name))
    task_rows: list[Row] = [
        {
            "name": t.name,
            "module": t.module,
            "file": t.file,
            "line": t.node.lineno,
            "kind": t.kind,
            "instance": t.instance.qualname,
            "schedule": t.schedule,
            "retries": t.retries,
            "envelope": envelope(index, t),
            "body_lines": body_lines(t.node),
            "registered": reg[f"{t.module}:{t.name}"],
            "params": params(t.node),
        }
        for t in tasks
    ]
    producer_rows: list[Row] = [
        {"qualname": q, "layer": producer_layer(q, f), "file": f, "line": line}
        for q, (f, line) in sorted(graph.producers.items())
    ]
    task_params = {t.name: params(t.node) for t in tasks}
    edge_rows: list[Row] = [
        {
            "kind": e.kind,
            "producer": e.producer,
            "consumer": e.consumer,
            "file": e.file,
            "lines": sorted(e.lines),
            "guarded": e.guarded,
            "message_class": e.message_class,
            "args": task_params.get(e.consumer, []) if e.kind in TASK_EDGE_KINDS else [],
        }
        for e in edges
    ]
    hits = edge_hits(index, graph, edges, opts) + task_hits(index, graph, edges, reg)
    hits.sort(key=lambda h: (str(h["code"]), str(h["subject"])))
    unresolved = sorted(
        graph.unresolved,
        key=lambda u: (str(u["file"]), int(str(u["line"])), str(u["reason"]), str(u["name"])),
    )
    counts: dict[str, int] = {
        "node.task": sum(t.kind == "task" for t in tasks),
        "node.periodic": sum(t.kind == "periodic" for t in tasks),
        "node.producer": len(producer_rows),
    }
    counts.update({f"edge.{k}": sum(e.kind == k for e in edges) for k in EDGE_KINDS})
    counts.update({f"class.{c}": sum(e.message_class == c for e in edges) for c in CLASSES})
    counts.update({f"rule.{r}": sum(h["code"] == r for h in hits) for r in RULES})
    counts["unresolved_dispatch"] = len(unresolved)
    result: Row = {
        "schema_version": SCHEMA_VERSION,
        "header": header,
        "nodes": {"tasks": task_rows, "producers": producer_rows},
        "edges": edge_rows,
        "unresolved_dispatch": unresolved,
        "hits": hits,
        "counts": counts,
    }
    return result, task_graph(index, edges)


def paged(lines: list[str], limit: int, offset: int) -> list[str]:
    shown = lines[offset : offset + limit]
    rest = len(lines) - offset - len(shown)
    if rest > 0:
        shown.append(f"{rest} more (--offset {offset + len(shown)})")
    return shown


def summary_lines(result: Row) -> list[str]:
    counts = result["counts"]
    assert isinstance(counts, dict)
    lines = [f"{key}: {counts[key]}" for key in sorted(counts)]
    if counts["class.EVENT"] == 0:
        lines.append("no publish mechanism found")
    return lines


def rows_of(result: Row, key: str) -> list[Row]:
    rows = result[key]
    assert isinstance(rows, list)
    return rows


def rule_lines(result: Row, code: str) -> list[str]:
    return [
        f"{h['subject']}  {h['file']}:{h['line']}"
        for h in rows_of(result, "hits")
        if h["code"] == code
    ]


def node_lines(result: Row, name: str) -> list[str]:
    def matches(endpoint: object) -> bool:
        text = str(endpoint)
        return name in (text, text.partition(":")[2])

    return [
        f"{e['kind']} {e['producer']} -> {e['consumer']}"
        for e in rows_of(result, "edges")
        if matches(e["producer"]) or matches(e["consumer"])
    ]


def chain_lines(adjacency: dict[str, set[str]]) -> list[str]:
    paths = [f"path  {' -> '.join(p)}" for p in longest_paths(adjacency)]
    loops = [f"cycle {' -> '.join(c)}" for c in cycles(adjacency)]
    return paths + loops


def check_lines(result: Row, baseline_path: Path) -> list[str]:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalysisError(f"cannot read baseline {baseline_path}: {exc}") from exc
    if not isinstance(baseline, dict):
        raise AnalysisError(f"baseline {baseline_path} is not an event-graph output")
    old_hits = {(h["code"], h["subject"]) for h in baseline.get("hits", [])}
    old_rows = {
        (u["reason"], u["producer"], u["name"]) for u in baseline.get("unresolved_dispatch", [])
    }
    new = [
        f"new hit: {h['code']} {h['subject']}"
        for h in rows_of(result, "hits")
        if (h["code"], h["subject"]) not in old_hits
    ]
    new += [
        f"new unresolved_dispatch: {u['reason']} {u['producer']} {u['name']}"
        for u in rows_of(result, "unresolved_dispatch")
        if (u["reason"], u["producer"], u["name"]) not in old_rows
    ]
    return new


def qualified(value: str) -> tuple[str, str]:
    module, sep, qualname = value.partition(":")
    if not (sep and module and qualname):
        raise argparse.ArgumentTypeError(f"expected MODULE:QUALNAME, got {value!r}")
    return module, qualname


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--root", default=".")
    parser.add_argument("--out", default=None)
    parser.add_argument("--guard-fn", default=DEFAULT_GUARD)
    parser.add_argument("--publish-fn", action="append", default=[])
    parser.add_argument("--subscribe-fn", action="append", default=[])
    parser.add_argument("--ev01-scope", action="append", default=[])
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--rule", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--chains", action="store_true")
    parser.add_argument("--check", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    for value in [args.guard_fn, *args.publish_fn, *args.subscribe_fn]:
        try:
            qualified(value)
        except argparse.ArgumentTypeError as exc:
            parser.error(str(exc))
    return args


def header_of(args: argparse.Namespace, out: Path) -> Row:
    options: Row = {
        "root": ".",
        "out": out.name,
        "guard_fn": args.guard_fn,
        "publish_fn": sorted(args.publish_fn),
        "subscribe_fn": sorted(args.subscribe_fn),
        "ev01_scope": sorted(args.ev01_scope),
        "summary": args.summary,
        "rule": args.rule,
        "node": args.node,
        "chains": args.chains,
        "check": Path(args.check).name if args.check else None,
        "limit": args.limit,
        "offset": args.offset,
    }
    return {"options": options, "excluded_dirs": [".*", *sorted(EXCLUDED_DIRS)]}


def report(args: argparse.Namespace, result: Row, adjacency: dict[str, set[str]]) -> list[str]:
    lines: list[str] = []
    if args.summary:
        lines += summary_lines(result)
    if args.rule:
        lines += paged(rule_lines(result, args.rule), args.limit, args.offset)
    if args.node:
        lines += paged(node_lines(result, args.node), args.limit, args.offset)
    if args.chains:
        lines += paged(chain_lines(adjacency), args.limit, args.offset)
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root)
    if not root.is_dir():
        print(f"event_graph: root is not a directory: {args.root}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else root / "audit" / "event-graph.json"
    opts = Options(
        guard=qualified(args.guard_fn),
        publish=frozenset(qualified(v) for v in args.publish_fn),
        subscribe=frozenset(qualified(v) for v in args.subscribe_fn),
        ev01_scope=frozenset(args.ev01_scope),
    )
    try:
        result, adjacency = analyse(root, opts, header_of(args, out))
        out.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(result, sort_keys=True, indent=2) + "\n"
        out.write_text(text, encoding="utf-8", newline="\n")
        lines = report(args, result, adjacency)
        new = check_lines(result, Path(args.check)) if args.check else []
    except AnalysisError as exc:
        print(f"event_graph: {exc}", file=sys.stderr)
        return 2
    for line in [*lines, *new]:
        print(line)
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
