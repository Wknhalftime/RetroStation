"""Phase 0b gate (SPEC 2026-10-28): ``scripts/gate.py`` and the tools' guards (S8, S9, S10, E6,
E7).

- ``clone --source-dsn <dev> [--pg-bin <dir>] [--branch <name>]`` copies the source into this
  branch's copy, ``copy_name(branch)`` (``retrostation_bench_gate_<branch>``, in
  ``retrostation_test_ts`` when it exists), ANALYZEs it and marks it as a gate copy
  (``mark_copy(dsn, source=..., copied_at=...)``, which the recorders put in their headers). It
  prints the source's name and ``<n> plays``. The branch defaults to the checked-out one.
- ``migrate --dsn``; ``run-task <dotted.path> [args...] [--dsn]``, which prints one
  ``stubbed hand-off: <task name>`` line per hand-off it kept off the queue; ``drop [--branch]``.
- ``round-trip --base <worktree> --source-dsn <dev> [--branch] [--out-dir] [--sample] [--allow]
  [--mode ...] [--tasks ...] [--apply ...]`` is ``poe gate``. It prints a ``gate: <step>`` line
  as each step starts (``clone``, ``base``, ``migrate``, ``task``, ``apply``, ``branch``,
  ``compare``) and every command it runs. An apply script runs as ``<script> --dsn <copy>`` with
  ``GATE_RUN_DIR`` naming the run's folder, where it may write ``work_map.json``. It exits 0
  only when the four compares find nothing that is not allowed.

The copies these tests make come from a source of their own, ``retrostation_gatefixture<db>``
(a source may hold neither "test" nor "bench" in its name), reset and reused by every run like
the test databases. Each test drops the copies it makes, through ``drop``.
"""

from __future__ import annotations

import re
import subprocess
import textwrap
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest
from psycopg import sql as pg_sql
from psycopg.rows import dict_row

from backend.db.migrations import run_migrations
from tests.integration import stream_seed as seed
from tests.scripts.gate_world import (
    ROOT,
    Conn,
    app_client,
    build_world,
    dbname_of,
    decoy_dsn,
    ingest,
    load_tool,
    read_json,
    run_guarded,
    with_dbname,
    write_json,
)

pytestmark = pytest.mark.usefixtures("queue_in_memory")

RECORDINGS = ("airplay", "golden", "dry-artist", "dry-identity")
ONE_PLAY = [("SOME ACT", "SOME SONG", "1995-03-14 08:00:00")]
ARTIST_TASK = "backend.tasks.artist_matching_tasks.artist_matching_task"


@pytest.fixture(scope="module")
def gate() -> Any:
    return load_tool("gate")


def admin_of(dsn: str) -> str:
    return with_dbname(dsn, "postgres")


def database_exists(dsn: str, name: str) -> bool:
    with psycopg.connect(admin_of(dsn), autocommit=True) as conn:
        found = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return found is not None


def fresh_database(dsn: str, name: str) -> str:
    """*name* on *dsn*'s server, created when missing, its public schema reset, migrated."""
    with psycopg.connect(admin_of(dsn), autocommit=True) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is None:
            conn.execute(pg_sql.SQL("CREATE DATABASE {}").format(pg_sql.Identifier(name)))
    target = with_dbname(dsn, name)
    with psycopg.connect(target, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(pg_sql.SQL("COMMENT ON DATABASE {} IS NULL").format(pg_sql.Identifier(name)))
    with psycopg.connect(target) as conn:
        run_migrations(conn)
        conn.commit()
    return target


@pytest.fixture
def source(bench: str) -> str:
    """A source database the gate accepts: no "test" or "bench" in its name."""
    suffix = dbname_of(bench).removeprefix("retrostation_test").replace("_bench", "") or "_main"
    return fresh_database(bench, f"retrostation_gatefixture{suffix}")


@pytest.fixture
def branch(gate: Any, bench: str) -> Iterator[str]:
    """A branch name of this test's own; its copy is dropped afterwards."""
    name = f"gate-test-{uuid4().hex[:8]}"
    yield name
    gate.main(["drop", "--branch", name])
    assert not database_exists(bench, gate.copy_name(name))


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every HTTP request is recorded and refused, so a task run once can't reach MusicBrainz."""
    touched: list[str] = []

    def send(_client: object, request: httpx.Request, **_kwargs: object) -> httpx.Response:
        touched.append(str(request.url))
        raise AssertionError("the gate reached the network")

    monkeypatch.setattr(httpx.Client, "send", send)
    return touched


def plays_in(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        found = conn.execute("SELECT count(*) FROM play_events").fetchone()
    assert found is not None
    return int(found[0])


def seed_plays(dsn: str) -> int:
    """Two stations' plays through the locked shared seed; returns the play count."""
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        for station_format in ("AC", None):
            playlist = seed.playlist(conn, seed.station(conn, station_format))
            seed.matched_play(conn, playlist, seed.at("08:00"), seed.mastered_file(conn))
            seed.play(conn, playlist, seed.identity(conn, status="pending"), seed.at("08:04"))
        conn.commit()
    return plays_in(dsn)


# --- S8 ------------------------------------------------------------------------------------------


@pytest.mark.integration
def test_refuses_test_bench_or_same_source(gate: Any, bench: str, process_env: None) -> None:
    """S8: a source that is a test or bench database, or the copy itself, is refused before
    anything is dropped or created."""
    name = f"gate-test-{uuid4().hex[:8]}"
    target = with_dbname(bench, gate.copy_name(name))
    for refused in (
        with_dbname(bench, "retrostation_test"),
        with_dbname(bench, "retrostation_bench_enrich"),
        target,
    ):
        with pytest.raises(SystemExit) as refusal:
            gate.main(["clone", "--source-dsn", refused, "--branch", name])
        assert refusal.value.code not in (0, None)
    assert not database_exists(bench, gate.copy_name(name))


@pytest.mark.integration
def test_clone_copies_analyzes_and_marks(
    gate: Any,
    bench: str,
    source: str,
    branch: str,
    process_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """S8: the copy holds the source's rows, ANALYZEd, in the test tablespace when it exists,
    marked with its source and copy time; pg_dump runs with --lock-wait-timeout; the source's
    name and play count are printed; clone can run again."""
    played = seed_plays(source)
    commands: list[list[str]] = []
    real_popen = subprocess.Popen

    def recording_popen(args: Any, *rest: Any, **kwargs: Any) -> Any:
        commands.append([str(a) for a in args])
        return real_popen(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    assert gate.main(["clone", "--source-dsn", source, "--branch", branch]) == 0

    copy = with_dbname(bench, gate.copy_name(branch))
    assert plays_in(copy) == played
    printed = capsys.readouterr().out
    assert dbname_of(source) in printed
    assert re.search(rf"\b{played} plays\b", printed)
    dumps = [c for c in commands if Path(c[0]).stem == "pg_dump"]
    assert dumps
    assert all(any(arg.startswith("--lock-wait-timeout") for arg in dump) for dump in dumps)
    with psycopg.connect(copy, row_factory=dict_row) as conn:
        # ANALYZE writes column statistics; a restore's index builds do not.
        stats = conn.execute(
            "SELECT count(*) AS n FROM pg_stats WHERE tablename = 'play_events'"
        ).fetchone()
        assert stats is not None and stats["n"] > 0
        place = conn.execute(
            """SELECT t.spcname FROM pg_database d
               JOIN pg_tablespace t ON t.oid = d.dattablespace
               WHERE d.datname = current_database()"""
        ).fetchone()
        wanted = conn.execute(
            "SELECT 1 FROM pg_tablespace WHERE spcname = 'retrostation_test_ts'"
        ).fetchone()
        assert place is not None
        assert place["spcname"] == ("retrostation_test_ts" if wanted else "pg_default")

    snapshot = load_tool("airplay_snapshot")
    assert snapshot.main(["--dsn", copy, "--out", str(tmp_path / "s.json")]) == 0
    header = read_json(tmp_path / "s.json")["header"]
    assert header["copy"]["source"] == dbname_of(source)
    datetime.fromisoformat(header["copy"]["copied_at"])

    assert gate.main(["clone", "--source-dsn", source, "--branch", branch]) == 0  # again
    assert plays_in(copy) == played


@pytest.mark.integration
def test_failed_clone_leaves_no_copy(
    gate: Any,
    bench: str,
    source: str,
    branch: str,
    process_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S8 (Failure): a clone whose restore fails, after the copy was created, drops it."""
    seed_plays(source)
    real_run = subprocess.run
    restores: list[str] = []

    def failing_restore(args: Any, *rest: Any, **kwargs: Any) -> Any:
        if Path(str(args[0])).stem == "pg_restore":
            restores.append(str(args[0]))
            assert database_exists(bench, gate.copy_name(branch))  # the copy was made
            return subprocess.CompletedProcess(args, returncode=1)
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", failing_restore)
    with pytest.raises(SystemExit):
        gate.main(["clone", "--source-dsn", source, "--branch", branch])
    assert restores
    assert not database_exists(bench, gate.copy_name(branch))


@pytest.mark.integration
def test_migrate_runs_this_trees_migrations(gate: Any, bench: str, process_env: None) -> None:
    """S8: migrate applies every migration of the checked-out tree."""
    target = with_dbname(bench, f"{dbname_of(bench)}_migrate")
    fresh_database(bench, dbname_of(target))
    with psycopg.connect(target, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")

    assert gate.main(["migrate", "--dsn", target]) == 0

    files = sorted(p.stem for p in (ROOT / "backend" / "db" / "migrations").glob("*.sql"))
    with psycopg.connect(target) as conn:
        applied = [r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()]
    assert sorted(applied) == files


@pytest.mark.integration
def test_run_task_runs_one_task_body(
    gate: Any, conn: Conn, bench: str, tool_env: None, no_network: list[str]
) -> None:
    """S8: run-task runs one task body in process (``call_local``) on the given database."""
    playlist = ingest(conn, seed.station(conn, "AC"), ONE_PLAY, "rt")

    assert gate.main(["run-task", ARTIST_TASK, str(playlist), "--dsn", bench]) == 0

    status = conn.execute("SELECT match_status FROM broadcast_artists").fetchone()
    assert status is not None and status["match_status"] != "pending"  # the task ran
    assert no_network == []


# --- E6, E7 --------------------------------------------------------------------------------------


@pytest.mark.integration
def test_per_branch_names(gate: Any, bench: str, source: str, process_env: None) -> None:
    """E6: each branch has its own copy, named for it; cloning or dropping one branch's copy
    never touches another's."""
    names = [
        gate.copy_name("feat/artist-model-phase-0b"),
        gate.copy_name("feat/artist-model-phase-1a"),
        gate.copy_name("feat/" + "a-very-long-branch-name-" * 3 + "one"),
        gate.copy_name("feat/" + "a-very-long-branch-name-" * 3 + "two"),
    ]
    assert len(set(names)) == len(names)
    for name in names:
        assert name.startswith("retrostation_bench_gate_")
        assert len(name.encode()) <= 63
        assert name == name.lower() and all(c.isalnum() or c == "_" for c in name)

    seed_plays(source)
    mine, theirs = f"gate-test-{uuid4().hex[:8]}", f"gate-test-{uuid4().hex[:8]}"
    try:
        assert gate.main(["clone", "--source-dsn", source, "--branch", theirs]) == 0
        assert gate.main(["clone", "--source-dsn", source, "--branch", mine]) == 0
        assert database_exists(bench, gate.copy_name(theirs))
        gate.main(["drop", "--branch", mine])
        assert not database_exists(bench, gate.copy_name(mine))
        assert database_exists(bench, gate.copy_name(theirs))
    finally:
        gate.main(["drop", "--branch", mine])
        gate.main(["drop", "--branch", theirs])


@pytest.mark.integration
def test_no_handoff_reaches_huey(
    gate: Any,
    conn: Conn,
    bench: str,
    tool_env: None,
    no_network: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """E7: a task run once by the gate hands off to nothing: every hand-off is stubbed, so
    nothing reaches a queue (here the in-memory one that stands in for ``huey.db``)."""
    from backend.tasks.huey_app import huey

    playlist = ingest(conn, seed.station(conn, "AC"), ONE_PLAY, "e7")
    huey.flush()
    assert huey.pending_count() == 0

    tasks = (
        (ARTIST_TASK, str(playlist)),
        ("backend.tasks.mb_enrichment_tasks.mb_enrichment_task",),
        ("backend.tasks.library_enrichment_tasks.library_enrichment_task",),
    )
    printed: list[str] = []
    for task in tasks:
        assert gate.main(["run-task", *task, "--dsn", bench]) == 0
        assert huey.pending_count() == 0, task
        printed.append(capsys.readouterr().out)
    # the artist stage hands on to identity matching: stubbed, and said so
    assert "stubbed hand-off: identity_matching_task" in printed[0]
    assert no_network == []


# --- S10 -----------------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_never_opens_another_dsn(
    gate: Any,
    conn: Conn,
    bench: str,
    tmp_path: Path,
    tool_env: None,
) -> None:
    """S10: every tool points the app at its bench DSN (and sets STREAM_ENABLED=false and
    PYTEST_XDIST_WORKER=gate) before it imports backend code, and opens no other database,
    though DATABASE_URL names one (a guard connection factory, ``gate_guard.py``); a DSN
    without "bench" is refused."""
    playlist = ingest(conn, seed.station(conn, "AC"), ONE_PLAY, "s10")
    sample = write_json(tmp_path / "sample.json", {"artists": {}, "works": {}, "log_names": {}})
    out = {name: str(tmp_path / f"{name}.json") for name in ("a", "b", "c", "d", "x", "y", "z")}
    runs = [
        ("airplay_snapshot.py", "--dsn", bench, "--out", out["a"]),
        ("dry_run_matching.py", "--dsn", bench, "--stage", "artist", "--out", out["b"]),
        ("dry_run_matching.py", "--dsn", bench, "--stage", "identity", "--out", out["c"]),
        ("golden_responses.py", "--dsn", bench, "--sample", str(sample), "--out", out["d"]),
        ("gate.py", "migrate", "--dsn", bench),
        ("gate.py", "run-task", ARTIST_TASK, str(playlist), "--dsn", bench),
    ]
    for script, *args in runs:
        done = run_guarded(bench, script, *args)
        assert done.returncode == 0, (script, done.stderr[-3000:])

    decoy = decoy_dsn(bench)  # no "bench", and no such database: nothing can run on it
    mb_task = "backend.tasks.mb_enrichment_tasks.mb_enrichment_task"
    for tool, args in (
        ("airplay_snapshot", ["--dsn", decoy, "--out", out["x"]]),
        ("dry_run_matching", ["--dsn", decoy, "--stage", "artist", "--out", out["y"]]),
        ("golden_responses", ["--dsn", decoy, "--sample", str(sample), "--out", out["z"]]),
        ("gate", ["migrate", "--dsn", decoy]),
        ("gate", ["run-task", mb_task, "--dsn", decoy]),
    ):
        with pytest.raises(SystemExit, match="bench"):
            load_tool(tool).main(args)


# --- S9 ------------------------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.slow
def test_round_trip_on_seed(
    gate: Any,
    bench: str,
    source: str,
    branch: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tool_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """S9: ``poe gate`` on a base tree identical to the branch: clone, the four recordings from
    the base, migrate, the run-once tasks, the apply scripts, the four again from the branch,
    and the four compares, in that order, with no differences. --mode and --allow reach the
    compares, and so does the work map an apply script wrote.
    """
    with psycopg.connect(source, row_factory=dict_row) as src, app_client(source) as client:
        world = build_world(src, client, tmp_path, monkeypatch)
    sample = write_json(
        tmp_path / "sample.json",
        {
            "artists": {"x": [world.alpha_artist]},
            "works": {"x": [world.first_work]},
            "log_names": {"x": [str(world.delta)]},
        },
    )
    apply = tmp_path / "apply_noop.py"
    apply.write_text(
        textwrap.dedent(
            """
            import json, os, sys
            assert sys.argv[1] == "--dsn" and "bench" in sys.argv[2]
            out = os.path.join(os.environ["GATE_RUN_DIR"], "work_map.json")
            with open(out, "w", encoding="utf-8") as f:
                json.dump({}, f)
            print("applied")
            """
        ),
        encoding="utf-8",
    )
    allow = tmp_path / "allow.txt"
    allow.write_text("names no-such-name nothing to allow (round-trip test)\n", encoding="utf-8")
    out_dir = tmp_path / "run"

    code = gate.main(
        [
            "round-trip",
            "--base", str(ROOT),
            "--source-dsn", source,
            "--branch", branch,
            "--out-dir", str(out_dir),
            "--sample", str(sample),
            "--allow", str(allow),
            "--mode", "missing-matches=only-down",
            "--tasks", "backend.tasks.progress_prune_tasks.progress_prune_task",
            "--apply", str(apply),
        ]
    )  # fmt: skip

    printed = capsys.readouterr().out
    assert code == 0, printed
    for name in RECORDINGS:
        assert (out_dir / "base" / f"{name}.json").exists()
        assert (out_dir / "branch" / f"{name}.json").exists()
        report = read_json(out_dir / f"compare-{name}.json")
        assert report == {"refused": None, "failed": [], "allowed": []}, name
    steps = ("clone", "base", "migrate", "task", "apply", "branch", "compare")
    starts = [printed.index(f"gate: {step}") for step in steps]
    assert starts == sorted(starts)
    compares = [line for line in printed.splitlines() if "--compare" in line]
    assert len(compares) == 4
    work_map = str(out_dir / "work_map.json")
    for line in compares:
        assert "--allow" in line and str(allow) in line
        assert "missing-matches=only-down" in line
        assert "--work-map" in line and work_map in line
    assert database_exists(bench, gate.copy_name(branch))  # left for inspection; `drop` ends it
