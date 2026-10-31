"""The gate (phase 0b SPEC 2026-10-28, S8, S9, E6, E7): a copy of the dev DB per branch, the
steps a PR's before and after run on it, and one command that runs both and compares them.

    python scripts/gate.py clone --source-dsn <dev> [--pg-bin <dir>] [--branch <name>]
    python scripts/gate.py migrate [--dsn <copy>]
    python scripts/gate.py run-task <dotted.path> [args ...] [--dsn <copy>]
    python scripts/gate.py drop [--branch <name>] [--server-dsn <dsn>]
    python scripts/gate.py round-trip --base <worktree> [--source-dsn <dev>] [--branch <name>]
        [--out-dir <dir>] [--sample <file>] [--allow <file>] [--mode <section>=<mode> ...]
        [--tasks <dotted.path> ...] [--apply <script> ...] [--pg-bin <dir>]

``poe gate`` is ``round-trip``. The copy of a branch is ``copy_name(branch)``; ``--dsn`` and
``--branch`` default to this checkout's branch. The dev DB is only ever read, by pg_dump in
``clone``; every other step refuses a DSN without "bench" (S10). A DSN's password goes in
PGPASSWORD for the run, never on a command line.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import psycopg
from psycopg import sql as pg_sql

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_common  # noqa: E402

COPY_PREFIX = "retrostation_bench_gate_"
_MAX_NAME = 63  # PostgreSQL's identifier limit, in bytes
TEST_TABLESPACE = "retrostation_test_ts"
LOCK_WAIT = "--lock-wait-timeout=60s"
# The recordings a round trip makes, from the base tree and then from the branch.
RECORDINGS = ("airplay", "golden", "dry-artist", "dry-identity")
_RECORDING_TOOLS = {
    "airplay": "airplay_snapshot",
    "golden": "golden_responses",
    "dry-artist": "dry_run_matching",
    "dry-identity": "dry_run_matching",
}


def copy_name(branch: str) -> str:
    """The branch's copy: ``retrostation_bench_gate_<branch>_<hash>``, lowercase letters,
    digits and underscores, at most 63 bytes. The hash of the branch's exact name keeps apart
    two branches whose names read alike here (``feat/x-y``, ``feat/x_y``, a long pair)."""
    slug = re.sub(r"[^a-z0-9]+", "_", branch.lower()).strip("_")
    digest = hashlib.sha1(branch.encode()).hexdigest()[:8]
    room = _MAX_NAME - len(COPY_PREFIX) - len(digest) - 1
    return f"{COPY_PREFIX}{slug[:room].rstrip('_')}_{digest}"


def current_branch() -> str:
    done = subprocess.run(
        ["git", "-C", str(gate_common.ROOT), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    branch = done.stdout.strip()
    if branch == "HEAD":
        raise SystemExit("this checkout is on a detached HEAD: name the copy with --branch")
    return branch


def _dev_dsn() -> str:
    """The dev stack's database: the settings' default (the stack sets no DATABASE_URL, and
    the main checkout's .env names none; a DATABASE_URL in .env would not be seen here)."""
    gate_common.set_process_guards()  # this loads backend code (S10)
    from backend.config import Settings

    return str(Settings.model_fields["database_url"].default)


def _server_dsn(explicit: str | None) -> str:
    """A DSN on the copies' server: *explicit*, else DATABASE_URL, else the dev stack's."""
    return explicit or os.environ.get("DATABASE_URL") or _dev_dsn()


@contextmanager
def password_in_env(*dsns: str) -> Iterator[list[str]]:
    """The DSNs without their password, which is in PGPASSWORD for the block: libpq and the
    pg_dump, pg_restore and tool children read it there, so no command line carries it."""
    bare: list[str] = []
    passwords: set[str] = set()
    for dsn in dsns:
        params = psycopg.conninfo.conninfo_to_dict(dsn)
        password = params.pop("password", None)
        if password:
            passwords.add(str(password))
        bare.append(psycopg.conninfo.make_conninfo(**{k: str(v) for k, v in params.items()}))
    if len(passwords) > 1:
        raise SystemExit("the DSNs carry different passwords; put one in PGPASSWORD")
    previous = os.environ.get("PGPASSWORD")
    if passwords:
        os.environ["PGPASSWORD"] = passwords.pop()
    try:
        yield bare
    finally:
        if previous is None:
            os.environ.pop("PGPASSWORD", None)
        else:
            os.environ["PGPASSWORD"] = previous


# --- clone, mark, drop (S8, E6) ------------------------------------------------------------------


def mark_copy(dsn: str, *, source: str, copied_at: str) -> None:
    """Mark *dsn*'s database as a gate copy of *source*, made at *copied_at* (ISO time). The
    recorders copy the marker into their headers, so two copies' files are never compared."""
    name = gate_common.require_bench(dsn)
    marker = json.dumps({gate_common.COPY_MARKER: {"source": source, "copied_at": copied_at}})
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            pg_sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                pg_sql.Identifier(name), pg_sql.Literal(marker)
            )
        )


def _drop(admin_dsn: str, name: str) -> None:
    gate_common.require_bench(gate_common.with_dbname(admin_dsn, name))
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(
            pg_sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(pg_sql.Identifier(name))
        )


def _tablespace(admin_dsn: str) -> str | None:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        found = admin.execute(
            "SELECT 1 FROM pg_tablespace WHERE spcname = %s", (TEST_TABLESPACE,)
        ).fetchone()
    return TEST_TABLESPACE if found else None


def _client_bin(admin_dsn: str, explicit: str | None) -> str | None:
    """*explicit*, else the client tools of the server's own major version where PostgreSQL
    installs them, else None (``bench_enrich`` then takes them from PATH). A newer pg_dump
    writes settings an older server refuses on restore (pg_dump 17's ``transaction_timeout``
    on a 16 server), and Debian's wrapper puts the newest client on PATH."""
    if explicit:
        return explicit
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        major = admin.info.server_version // 10000
    tool = "pg_dump.exe" if sys.platform == "win32" else "pg_dump"
    for folder in (
        f"/usr/lib/postgresql/{major}/bin",
        f"C:/Program Files/PostgreSQL/{major}/bin",
        f"D:/Program Files/PostgreSQL/{major}/bin",
    ):
        if (Path(folder) / tool).is_file():
            return folder
    return None


def _refuse_source(source_name: str, target: str) -> None:
    lowered = source_name.lower()
    if "test" in lowered or "bench" in lowered or lowered == target:
        raise SystemExit(
            f"refusing to copy {source_name!r}: a test or bench database, or the copy itself"
        )


def clone(source_dsn: str, branch: str, pg_bin: str | None) -> str:
    """Copy *source_dsn* into the branch's copy, ANALYZE it and mark it; returns its DSN.

    pg_dump takes one snapshot under ACCESS SHARE locks, waiting at most a minute for them.
    A clone that fails after the copy was created drops it.
    """
    source_name, target = gate_common.dbname_of(source_dsn), copy_name(branch)
    _refuse_source(source_name, target)
    copy_dsn = gate_common.with_dbname(source_dsn, target)
    admin_dsn = gate_common.with_dbname(source_dsn, "postgres")
    # No settings are read here (the DSNs are explicit), so DATABASE_URL is left alone.
    gate_common.set_process_guards()
    from bench_enrich import clone_database

    started = time.perf_counter()
    done = False
    try:
        clone_database(
            source_dsn,
            admin_dsn,
            target,
            _client_bin(admin_dsn, pg_bin),
            tablespace=_tablespace(admin_dsn),
            dump_options=(LOCK_WAIT,),
        )
        copied = time.perf_counter()
        with psycopg.connect(copy_dsn, autocommit=True) as conn:
            conn.execute("ANALYZE")
            plays = conn.execute("SELECT count(*) FROM play_events").fetchone()
        mark_copy(copy_dsn, source=source_name, copied_at=datetime.now(UTC).isoformat())
        done = True
    finally:
        if not done:
            _drop(admin_dsn, target)
    count = plays[0] if plays else 0
    print(
        f"cloned {source_name} into {target}: {count} plays; copy "
        f"{copied - started:.1f} s, ANALYZE {time.perf_counter() - copied:.1f} s"
    )
    return copy_dsn


# --- migrate, run-task (S8, E7) ------------------------------------------------------------------


def migrate(dsn: str) -> None:
    """Run this tree's migrations on the copy."""
    gate_common.point_at(dsn)
    from backend.db.migrations import run_migrations

    with psycopg.connect(dsn) as conn:
        run_migrations(conn)
        conn.commit()
    print(f"migrated {gate_common.dbname_of(dsn)} from {gate_common.ROOT}")


def _announcing(name: str, stub: Callable[..., Any]) -> Callable[..., Any]:
    def announced(*args: Any, **kwargs: Any) -> Any:
        print(f"stubbed hand-off: {name}")
        return stub(*args, **kwargs)

    return announced


@contextmanager
def stubbed_hand_offs() -> Iterator[None]:
    """No hand-off reaches a queue (E7): bench_enrich's follow_on_task stubs the enrichment
    chain, and any other task call is kept off both Huey queues. Each says so."""
    from bench_enrich import follow_on_task

    from backend.tasks import artist_linking_tasks, matching_recheck_tasks, mb_enrichment_tasks
    from backend.tasks.cue_huey_app import cue_huey
    from backend.tasks.huey_app import huey

    def kept_off(task: Any) -> None:
        print(f"stubbed hand-off: {str(getattr(task, 'name', task)).rsplit('.', 1)[-1]}")

    with follow_on_task(run=False), ExitStack() as stack:
        # Named as strings: reading a task into a variable is an unresolved dispatch in the
        # event graph (as follow_on_task notes).
        for module, name in (
            (mb_enrichment_tasks, "mb_enrichment_task"),
            (matching_recheck_tasks, "rematch_undecided_task"),
            (artist_linking_tasks, "link_local_artists_task"),
        ):
            stack.enter_context(
                mock.patch.object(module, name, _announcing(name, getattr(module, name)))
            )
        for queue in (huey, cue_huey):
            stack.enter_context(mock.patch.object(queue, "enqueue", kept_off))
        yield


def run_task(dotted: str, args: Sequence[str], dsn: str) -> None:
    """One task body in process (``call_local``) on the copy, its hand-offs stubbed."""
    gate_common.point_at(dsn)
    module_name, _, attribute = dotted.rpartition(".")
    task = getattr(importlib.import_module(module_name), attribute)
    started = time.perf_counter()
    with stubbed_hand_offs():
        result = task.call_local(*args)
    print(f"ran {dotted}{tuple(args)!r}: {result!r}; {time.perf_counter() - started:.1f} s")


# --- round trip (S9) -----------------------------------------------------------------------------


def _show(command: Sequence[str]) -> None:
    print(f"$ {subprocess.list2cmdline([str(part) for part in command])}", flush=True)


def _run_together(
    commands: Sequence[Sequence[Any]], *, cwd: Path, env: dict[str, str] | None = None
) -> list[int]:
    """Run *commands* at once, each echoed as it starts; then each one's output, indented, in
    order. The output goes through a file, so no child ever waits on a full pipe."""
    running: list[tuple[subprocess.Popen[bytes], Any]] = []
    with ExitStack() as files:
        for command in commands:
            _show(command)
            output = files.enter_context(tempfile.TemporaryFile())
            child = subprocess.Popen(
                [str(part) for part in command],
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            running.append((child, output))
        codes: list[int] = []
        for child, output in running:
            codes.append(child.wait())
            output.seek(0)
            for line in output.read().decode("utf-8", errors="replace").splitlines():
                print(f"    {line}")
    return codes


def _run(command: Sequence[Any], *, cwd: Path, env: dict[str, str] | None = None) -> int:
    """Run and echo one command; its output is printed after it, indented."""
    return _run_together([command], cwd=cwd, env=env)[0]


def _must(code: int, what: str) -> None:
    if code != 0:
        raise SystemExit(f"gate: {what} failed (exit {code})")


def _require_tools(tree: Path) -> None:
    for tool in sorted({"gate", *_RECORDING_TOOLS.values()}):
        if not (tree / "scripts" / f"{tool}.py").is_file():
            raise SystemExit(
                f"{tree} has no scripts/{tool}.py: for a 0-phase PR, base the gate on a second "
                "worktree of the branch"
            )


def _recording_commands(tree: Path, copy: str, out: Path, sample: Path) -> dict[str, list[Any]]:
    scripts = tree / "scripts"
    extra = {
        "airplay": [],
        "golden": ["--sample", sample],
        "dry-artist": ["--stage", "artist"],
        "dry-identity": ["--stage", "identity"],
    }
    return {
        name: [
            sys.executable,
            scripts / f"{_RECORDING_TOOLS[name]}.py",
            "--dsn",
            copy,
            *extra[name],
            "--out",
            out / f"{name}.json",
        ]
        for name in RECORDINGS
    }


def _migrate_from(tree: Path, copy: str) -> None:
    command = [sys.executable, tree / "scripts" / "gate.py", "migrate", "--dsn", copy]
    _must(_run(command, cwd=tree), f"migrate from {tree}")


def _record(tree: Path, copy: str, out: Path, sample: Path) -> None:
    """The four recordings from *tree*: the snapshot alone (its timing record must not share
    the machine), then the other three at once (they only read)."""
    _require_tools(tree)
    commands = _recording_commands(tree, copy, out, sample)
    _must(_run(commands.pop("airplay"), cwd=tree), f"recording airplay from {tree}")
    codes = _run_together(list(commands.values()), cwd=tree)
    for name, code in zip(commands, codes, strict=True):
        _must(code, f"recording {name} from {tree}")


def _compare_command(name: str, out_dir: Path, args: argparse.Namespace) -> list[Any]:
    tool = gate_common.SCRIPTS / f"{_RECORDING_TOOLS[name]}.py"
    command: list[Any] = [sys.executable, tool, "--compare"]
    command += [out_dir / "base" / f"{name}.json", out_dir / "branch" / f"{name}.json"]
    command += ["--report", out_dir / f"compare-{name}.json"]
    if args.allow is not None:
        command += ["--allow", args.allow]
    if args.mode:
        command += ["--mode", *args.mode]
    work_map = out_dir / "work_map.json"
    if work_map.is_file():
        command += ["--work-map", work_map]
    return command


def _step(name: str) -> None:
    print(f"gate: {name}", flush=True)


def _branch_steps(args: argparse.Namespace, copy: str, out_dir: Path) -> None:
    """migrate, each run-once task, each apply script, from this tree."""
    gate_py = gate_common.SCRIPTS / "gate.py"
    root = gate_common.ROOT
    _step("migrate")
    _migrate_from(root, copy)
    _step("task")
    for task in args.tasks:
        command = [sys.executable, gate_py, "run-task", task, "--dsn", copy]
        _must(_run(command, cwd=root), f"run-task {task}")
    _step("apply")
    # An apply script's settings point at the copy too, never at the shell's DATABASE_URL.
    env = {**os.environ, "GATE_RUN_DIR": str(out_dir), "DATABASE_URL": copy}
    for script in args.apply:
        _must(_run([sys.executable, script, "--dsn", copy], cwd=root, env=env), f"apply {script}")


def _warn_unused(args: argparse.Namespace, out_dir: Path) -> None:
    """A --mode section none of the four recordings has, and an allowlist line no compare
    used: the round trip passes both to all four compares, which each ignore the others'."""
    sections: set[str] = {"timing"}
    used: set[tuple[str, str]] = set()
    for name in RECORDINGS:
        sections |= set(gate_common.read_doc(out_dir / "base" / f"{name}.json")["sections"])
        report = gate_common.read_doc(out_dir / f"compare-{name}.json")
        used |= {(entry["section"], entry["key"]) for entry in report["allowed"]}
    for pair in args.mode:
        if pair.partition("=")[0] not in sections:
            print(f"gate: warning: --mode {pair} names no section of the four recordings")
    allowed = gate_common.read_allowlist(args.allow)
    for section, key in sorted(allowed if isinstance(allowed, dict) else {}):
        if (section, key) not in used:
            print(f"gate: warning: the allowlist line {section} {key} matched no difference")


def round_trip(args: argparse.Namespace) -> int:
    """S9: clone; from the base tree, migrate (so a base with migrations the dev DB lacks can
    read the copy) and the four recordings; migrate, the run-once tasks and the apply scripts
    from the branch; the four again; the four compares. 0 only when the compares find nothing
    that is not allowed. The copy is left for inspection (``drop`` ends it)."""
    branch = args.branch or current_branch()
    # Absolute, as given: the steps run in two trees, and the commands name these paths.
    out_dir = (
        args.out_dir or gate_common.ROOT / "audit" / "snapshots" / copy_name(branch)
    ).absolute()
    sample = (args.sample or gate_common.ROOT / "audit" / "golden_sample.json").absolute()
    if args.allow is not None:
        args.allow = args.allow.absolute()
    (out_dir / "work_map.json").unlink(missing_ok=True)  # only this run's apply scripts write it
    with password_in_env(args.source_dsn or _dev_dsn()) as (source,):
        _step("clone")
        _show(["gate.py", "clone", "--source-dsn", source, "--branch", branch])
        copy = clone(source, branch, args.pg_bin)
        _step("base")
        base = args.base.absolute()
        _require_tools(base)
        _migrate_from(base, copy)
        _record(base, copy, out_dir / "base", sample)
        _branch_steps(args, copy, out_dir)
        _step("branch")
        _record(gate_common.ROOT, copy, out_dir / "branch", sample)
        _step("compare")
        compares = [_compare_command(name, out_dir, args) for name in RECORDINGS]
        codes = dict(zip(RECORDINGS, _run_together(compares, cwd=gate_common.ROOT), strict=True))
    _warn_unused(args, out_dir)
    for name, code in codes.items():
        print(f"gate: {name}: {'no differences that are not allowed' if code == 0 else 'FAILED'}")
    print(f"gate: the copy {copy_name(branch)} is left for inspection; `gate.py drop` ends it")
    return 0 if all(code == 0 for code in codes.values()) else 1


# --- the command line ----------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    clone_cmd = commands.add_parser("clone", help="copy the dev DB into this branch's copy")
    clone_cmd.add_argument("--source-dsn", required=True)
    clone_cmd.add_argument("--pg-bin", help="directory holding pg_dump and pg_restore")
    clone_cmd.add_argument("--branch")

    migrate_cmd = commands.add_parser("migrate", help="run this tree's migrations on a copy")
    migrate_cmd.add_argument("--dsn")

    task_cmd = commands.add_parser("run-task", help="run one task body on a copy")
    task_cmd.add_argument("task", help="dotted path, e.g. backend.tasks.x_tasks.x_task")
    task_cmd.add_argument("task_args", nargs="*")
    task_cmd.add_argument("--dsn")

    drop_cmd = commands.add_parser("drop", help="drop a branch's copy")
    drop_cmd.add_argument("--branch")
    drop_cmd.add_argument("--server-dsn", help="any DSN on the copies' server")

    trip = commands.add_parser("round-trip", help="poe gate: base and branch, compared")
    trip.add_argument("--base", type=Path, required=True, help="the base tree (a worktree)")
    trip.add_argument("--source-dsn", help="the dev DB (default: the dev stack's)")
    trip.add_argument("--branch")
    trip.add_argument("--out-dir", type=Path)
    trip.add_argument("--sample", type=Path)
    trip.add_argument("--allow", type=Path)
    trip.add_argument("--mode", action="extend", nargs="+", default=[])
    trip.add_argument("--tasks", action="extend", nargs="+", default=[])
    trip.add_argument("--apply", action="extend", nargs="+", default=[])
    trip.add_argument("--pg-bin")
    return parser


def _copy_dsn(explicit: str | None) -> str:
    """*explicit*, else this checkout's branch's copy on the copies' server."""
    if explicit:
        return explicit
    return gate_common.with_dbname(_server_dsn(None), copy_name(current_branch()))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "clone":
        with password_in_env(args.source_dsn) as (source,):
            clone(source, args.branch or current_branch(), args.pg_bin)
    elif args.command == "migrate":
        migrate(_copy_dsn(args.dsn))
    elif args.command == "run-task":
        run_task(args.task, args.task_args, _copy_dsn(args.dsn))
    elif args.command == "drop":
        name = copy_name(args.branch or current_branch())
        with password_in_env(_server_dsn(args.server_dsn)) as (server,):
            _drop(gate_common.with_dbname(server, "postgres"), name)
        print(f"dropped {name}")
    else:
        return round_trip(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
