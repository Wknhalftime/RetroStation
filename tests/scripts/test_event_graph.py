"""Spec for scripts/audit/event_graph.py, the event-graph analyzer (audit/eda/contract.md).

The fixture tree under fixtures/event_graph is parsed by the analyzer, never imported or run.
Every expectation here was derived by hand from those files; the case ids in the comments
key the rows of audit/eda/spec-expectations.jsonl.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "audit" / "event_graph.py"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "event_graph"
MAIN = _FIXTURES / "main"
ALT = _FIXTURES / "alt"
DUO = _FIXTURES / "duo"

GUARD = "evgapp.evg_enqueue_chain:enqueue_or_log"
MAIN_OPTS = [
    "--guard-fn",
    GUARD,
    "--publish-fn",
    "evgapp.evg_bus:publish",
    "--subscribe-fn",
    "evgapp.evg_bus:subscribe",
    "--ev01-scope",
    "unguarded_head_task",
]

CHAIN = "evgapp.tasks.evg_chain_tasks"
MISC = "evgapp.tasks.evg_misc_tasks"
API = "evgapp.routers.evg_api"
BUS = "evgapp.evg_bus"
SQL = "evgapp.services.evg_sql_service"
BENCH = "evgapp.scripts.evg_bench"


def _run(
    root: Path | str, out: Path, *args: str, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root), "--out", str(out), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        check=False,
    )


def _analyse(root: Path, out_dir: Path, *args: str) -> Any:
    out = out_dir / "event-graph.json"
    proc = _run(root, out, *args)
    assert proc.returncode == 0, proc.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _edge(graph: Any, kind: str, producer: str, consumer: str) -> Any:
    found = [
        e
        for e in graph["edges"]
        if (e["kind"], e["producer"], e["consumer"]) == (kind, producer, consumer)
    ]
    assert len(found) == 1, (kind, producer, consumer)
    return found[0]


def _hits(graph: Any, code: str) -> dict[str, Any]:
    return {h["subject"]: h for h in graph["hits"] if h["code"] == code}


def _task(graph: Any, name: str) -> Any:
    found = [t for t in graph["nodes"]["tasks"] if t["name"] == name]
    assert len(found) == 1, name
    return found[0]


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return _analyse(MAIN, tmp_path_factory.mktemp("main"), *MAIN_OPTS)


@pytest.fixture(scope="module")
def bare_graph(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The main tree with only the guard named: no publish/subscribe fns, no EV01 scope."""
    return _analyse(MAIN, tmp_path_factory.mktemp("bare"), "--guard-fn", GUARD)


@pytest.fixture(scope="module")
def alt_graph(tmp_path_factory: pytest.TempPathFactory) -> Any:
    return _analyse(ALT, tmp_path_factory.mktemp("alt"))


@pytest.fixture(scope="module")
def duo_graph(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Two Huey instances in one tree."""
    return _analyse(DUO, tmp_path_factory.mktemp("duo"))


# ---- rules EV01-EV10: one hit and one near-miss each ----


def test_ev01_unguarded_task_handoffs_hit_with_ruling_scope(graph: Any) -> None:
    # case EV01-hit, EV01-hit-unstated, EV01-fake-guard
    hits = _hits(graph, "EV01")
    assert set(hits) == {
        f"{CHAIN}:unguarded_head_task -> chain_tail_task",
        f"{CHAIN}:stray_head_task -> chain_mid_task",
        f"{MISC}:fake_guarded_task -> chain_tail_task",
    }
    assert {h["class"] for h in hits.values()} == {"ruling"}
    assert hits[f"{CHAIN}:unguarded_head_task -> chain_tail_task"]["ruling_scope"] == "stated"
    assert hits[f"{CHAIN}:stray_head_task -> chain_mid_task"]["ruling_scope"] == "unstated"
    assert hits[f"{MISC}:fake_guarded_task -> chain_tail_task"]["ruling_scope"] == "unstated"


def test_ev01_near_miss_guarded_handoff_and_router_enqueue(graph: Any) -> None:
    # case EV01-miss
    hits = _hits(graph, "EV01")
    assert f"{CHAIN}:chain_head_task -> chain_mid_task" not in hits
    assert f"{CHAIN}:fan_out_task -> chain_tail_task" not in hits
    assert f"{API}:start_batch -> chain_head_task" not in hits


def test_ev01_scope_is_unstated_without_the_option(bare_graph: Any) -> None:
    # case EV01-scope-default
    hits = _hits(bare_graph, "EV01")
    assert len(hits) == 3
    assert {h["ruling_scope"] for h in hits.values()} == {"unstated"}


def test_ev02_inline_execution_outside_tests(graph: Any) -> None:
    # case EV02-hit
    assert set(_hits(graph, "EV02")) == {
        f"{MISC}:resume_periodic -> chain_tail_task",
        f"{BENCH}:bench -> chain_tail_task",
        f"{BENCH}:bench_raw -> chain_tail_task",
    }


def test_ev02_near_miss_code_under_tests_is_not_scanned(graph: Any) -> None:
    # case EV02-miss
    assert not [e for e in graph["edges"] if e["file"].startswith("evgapp/tests/")]
    assert not [p for p in graph["nodes"]["producers"] if p["qualname"].startswith("evgapp.tests")]


def test_ev03_task_referenced_from_a_service(graph: Any) -> None:
    # case EV03-hit, EV03-miss (a task name in a service docstring is not a reference)
    assert set(_hits(graph, "EV03")) == {"evgapp.services.evg_catalog_service -> chain_tail_task"}


def test_ev04_cycle_in_the_enqueue_graph(graph: Any) -> None:
    # case EV04-hit, EV04-miss (chain_head -> chain_mid -> chain_tail is not a cycle)
    assert set(_hits(graph, "EV04")) == {"cyc_a_task -> cyc_b_task -> cyc_a_task"}


def test_ev05_query_edge_hits_and_results_false_does_not(graph: Any) -> None:
    # case EV05-hit-query, EV05-miss-results
    assert set(_hits(graph, "EV05")) == {f"{API}:wait_for_tail -> chain_tail_task"}


def test_ev05_results_left_at_huey_default_hits(alt_graph: Any) -> None:
    # case EV05-hit-results
    assert set(_hits(alt_graph, "EV05")) == {"evgalt.evg_alt_app:huey"}


def test_ev06_unregistered_module_and_tasks_without_producer(graph: Any) -> None:
    # case EV06-hit-module, EV06-hit-noproducer, EV06-miss (periodic, registered module)
    hits = _hits(graph, "EV06")
    assert set(hits) == {
        "evgapp.tasks.evg_orphan_tasks",
        "evgapp.tasks.evg_orphan_tasks:orphan_task",
        f"{CHAIN}:table_only_task",
    }
    assert hits["evgapp.tasks.evg_orphan_tasks"]["detail"] == "unregistered_module"
    orphan = hits["evgapp.tasks.evg_orphan_tasks:orphan_task"]
    assert orphan["detail"] == "no_producer"
    assert orphan["referenced_in_unresolved_dispatch"] is False
    assert hits[f"{CHAIN}:table_only_task"]["referenced_in_unresolved_dispatch"] is True


def test_ev07_task_params_that_are_not_plain_data(graph: Any) -> None:
    # case EV07-hit, EV07-miss (int, list[str], dict[str, int], str | None)
    assert set(_hits(graph, "EV07")) == {
        f"{MISC}:typed_task(blob)",
        f"{MISC}:typed_task(record)",
        f"{MISC}:typed_task(nested)",
    }


def test_ev08_body_lines_and_distinct_service_calls(graph: Any) -> None:
    # case EV08-hit, EV08-miss (a plain helper gets no row)
    hits = _hits(graph, "EV08")
    assert len(hits) == 19
    assert {h["class"] for h in hits.values()} == {"metric"}
    assert hits[f"{MISC}:service_heavy_task"]["value"] == {"body_lines": 5, "service_calls": 3}
    assert hits[f"{MISC}:sweep_periodic"]["value"] == {"body_lines": 3, "service_calls": 2}
    assert hits[f"{CHAIN}:chain_tail_task"]["value"] == {"body_lines": 1, "service_calls": 0}
    assert f"{MISC}:_log" not in hits


def test_ev09_polling_loop_and_state_reading_periodic(graph: Any) -> None:
    # case EV09-hit-poll, EV09-hit-periodic, EV09-miss (retry loop, query loop, constant test)
    assert set(_hits(graph, "EV09")) == {
        "evgapp.evg_poller:watch_progress",
        f"{MISC}:sweep_periodic",
    }


def test_ev10_distinct_tasks_named_per_producer(graph: Any) -> None:
    # case EV10-hit, EV10-miss
    hits = _hits(graph, "EV10")
    assert len(hits) == 23
    assert hits[f"{API}:kick_off_maintenance"]["value"] == {"tasks": 4}
    assert hits[f"{CHAIN}:fan_out_task"]["value"] == {"tasks": 2}
    assert hits[f"{API}:start_batch"]["value"] == {"tasks": 1}
    assert f"{BUS}:announce_batch" not in hits
    assert f"{API}:rebuild_catalog" not in hits


def test_hits_carry_no_severity(graph: Any) -> None:
    # case HIT-no-severity
    assert not [h for h in graph["hits"] if "severity" in h]


# ---- message classes: one case and one near-miss each ----


def test_command_is_an_enqueue_whose_result_is_not_read(graph: Any) -> None:
    # case CMD-hit
    edge = _edge(graph, "ENQUEUE", f"{API}:start_batch", "chain_head_task")
    assert edge["message_class"] == "COMMAND"
    assert edge["guarded"] is False


def test_command_near_miss_plain_function_calls_are_not_edges(graph: Any) -> None:
    # case CMD-miss (a service call; a plain function that shares a task's name)
    producers = {e["producer"] for e in graph["edges"]}
    assert f"{API}:rebuild_catalog" not in producers
    assert "evgapp.evg_support:local_call" not in producers


def test_query_reads_the_result_handle(graph: Any) -> None:
    # case QRY-hit
    edge = _edge(graph, "ENQUEUE", f"{API}:wait_for_tail", "chain_tail_task")
    assert edge["message_class"] == "QUERY"


def test_query_near_miss_get_on_a_dict(graph: Any) -> None:
    # case QRY-miss
    edge = _edge(graph, "ENQUEUE", f"{API}:enqueue_and_read_options", "chain_tail_task")
    assert edge["message_class"] == "COMMAND"


def test_event_huey_signal_handler(graph: Any) -> None:
    # case EVT-signal
    for signal in ("SIGNAL_COMPLETE", "SIGNAL_ERROR"):
        topic = f"huey-signal:{signal}"
        pub = _edge(graph, "PUBLISH", "evgapp.evg_huey_app:huey", topic)
        assert pub["message_class"] == "EVENT"
        sub = _edge(graph, "SUBSCRIBE", topic, "evgapp.tasks.evg_signal_tasks:on_task_done")
        assert sub["message_class"] is None


def test_event_sql_notify_and_listen(graph: Any) -> None:
    # case EVT-notify, EVT-notify-miss (notify_count is a column, not NOTIFY)
    assert (
        _edge(graph, "PUBLISH", f"{SQL}:announce_import", "pg-channel:imports_done")[
            "message_class"
        ]
        == "EVENT"
    )
    assert (
        _edge(graph, "PUBLISH", f"{SQL}:announce_scan", "pg-channel:scans_done")["message_class"]
        == "EVENT"
    )
    _edge(graph, "SUBSCRIBE", "pg-channel:imports_done", f"{SQL}:listen_imports")
    assert f"{SQL}:read_notify_count" not in {e["producer"] for e in graph["edges"]}


def test_event_named_publish_and_subscribe_functions(graph: Any) -> None:
    # case EVT-fn
    pub = _edge(graph, "PUBLISH", f"{BUS}:announce_batch", "topic:batch.finished")
    assert pub["message_class"] == "EVENT"
    _edge(graph, "SUBSCRIBE", "topic:batch.finished", f"{BUS}:on_batch_finished")


def test_event_near_miss_publish_fn_needs_the_option(bare_graph: Any) -> None:
    # case EVT-fn-miss
    endpoints = {e["producer"] for e in bare_graph["edges"]} | {
        e["consumer"] for e in bare_graph["edges"]
    }
    assert not [x for x in endpoints if x.startswith("topic:")]
    assert bare_graph["counts"]["class.EVENT"] == 4


def test_event_near_miss_publish_named_function_that_enqueues_is_a_command(graph: Any) -> None:
    # case EVT-miss-name
    edge = _edge(graph, "ENQUEUE", f"{API}:publish_batch_started", "chain_head_task")
    assert edge["message_class"] == "COMMAND"
    assert not [
        e
        for e in graph["edges"]
        if e["producer"] == f"{API}:publish_batch_started" and e["kind"] == "PUBLISH"
    ]


def test_no_publish_mechanism_is_a_result(alt_graph: Any, tmp_path: Path) -> None:
    # case EVT-none
    assert alt_graph["counts"]["class.EVENT"] == 0
    proc = _run(ALT, tmp_path / "event-graph.json", "--summary")
    assert proc.returncode == 0, proc.stderr
    assert "no publish mechanism found" in proc.stdout.splitlines()


# ---- edges, resolution, nodes ----


def test_deferred_enqueue(graph: Any) -> None:
    # case DEF-hit
    edge = _edge(graph, "ENQUEUE_DEFERRED", f"{API}:schedule_tail", "chain_tail_task")
    assert edge["message_class"] == "COMMAND"


def test_guard_takes_a_lambda_or_the_task_itself(graph: Any) -> None:
    # case GUARD-lambda, GUARD-value
    assert _edge(graph, "ENQUEUE", f"{CHAIN}:chain_head_task", "chain_mid_task")["guarded"] is True
    assert _edge(graph, "ENQUEUE", f"{CHAIN}:chain_mid_task", "chain_tail_task")["guarded"] is True
    assert len([e for e in graph["edges"] if e["producer"] == f"{CHAIN}:chain_head_task"]) == 1


def test_same_named_guard_in_another_module_is_not_the_guard(graph: Any) -> None:
    # case GUARD-fake
    assert (
        _edge(graph, "ENQUEUE", f"{MISC}:fake_guarded_task", "chain_tail_task")["guarded"] is False
    )


def test_repeated_sites_merge_into_one_edge(graph: Any) -> None:
    # case MERGE
    edge = _edge(graph, "ENQUEUE", f"{CHAIN}:fan_out_task", "chain_tail_task")
    assert edge["lines"] == [33, 34]
    assert edge["file"] == "evgapp/tasks/evg_chain_tasks.py"


def test_task_called_through_a_module_attribute_resolves(graph: Any) -> None:
    # case RESOLVE-modattr
    _edge(graph, "ENQUEUE", f"{API}:start_batch_via_module", "unguarded_head_task")


def test_edge_carries_consumer_argument_names_and_annotations(graph: Any) -> None:
    # case ARGS
    edge = _edge(graph, "ENQUEUE", f"{API}:ingest", "typed_task")
    assert edge["args"] == [
        {"name": "item_id", "annotation": "int"},
        {"name": "tags", "annotation": "list[str]"},
        {"name": "opts", "annotation": "dict[str, int]"},
        {"name": "label", "annotation": "str | None"},
        {"name": "blob", "annotation": "bytes"},
        {"name": "record", "annotation": "Track"},
        {"name": "nested", "annotation": "list[list[str]]"},
    ]


def test_poll_edge(graph: Any) -> None:
    # case POLL-hit, POLL-miss
    _edge(graph, "POLL", "evgapp.evg_poller:watch_progress", "table:progress_rows")
    polls = {e["producer"] for e in graph["edges"] if e["kind"] == "POLL"}
    assert polls == {"evgapp.evg_poller:watch_progress"}


def test_unresolved_dispatch_is_listed_never_dropped(graph: Any) -> None:
    # case UD-table, UD-getattr, UD-value
    rows = {(u["file"], u["line"], u["reason"], u["name"]) for u in graph["unresolved_dispatch"]}
    api = "evgapp/routers/evg_api.py"
    assert rows == {
        (api, 21, "task_table", "chain_head_task"),
        (api, 21, "task_table", "table_only_task"),
        (api, 75, "getattr", None),
        (api, 83, "task_as_value", "chain_head_task"),
        ("evgapp/evg_bus.py", 28, "subscribe_handler", None),
    }
    producers = {u["producer"] for u in graph["unresolved_dispatch"] if u["line"] == 21}
    assert producers == {f"{API}:<module>"}


def test_unresolvable_subscribe_handler_is_unresolved_not_an_edge(graph: Any) -> None:
    # case UD-subscribe-handler
    subscribers = {
        e["consumer"]
        for e in graph["edges"]
        if e["kind"] == "SUBSCRIBE" and e["producer"] == "topic:batch.finished"
    }
    assert subscribers == {f"{BUS}:on_batch_finished"}
    rows = [u for u in graph["unresolved_dispatch"] if u["reason"] == "subscribe_handler"]
    assert [u["producer"] for u in rows] == [f"{BUS}:wire_inline"]


def test_task_nodes(graph: Any) -> None:
    # case NODE
    tail = _task(graph, "chain_tail_task")
    assert (tail["module"], tail["kind"], tail["schedule"]) == (CHAIN, "task", None)
    assert (tail["retries"], tail["envelope"], tail["registered"]) == (0, "none", True)
    assert tail["instance"] == "evgapp.evg_huey_app:huey"
    sweep = _task(graph, "sweep_periodic")
    assert (sweep["kind"], sweep["schedule"]) == ("periodic", 'crontab(minute="*/4")')
    own = _task(graph, "own_envelope_task")
    assert (own["retries"], own["envelope"]) == (2, "own")
    assert _task(graph, "run_envelope_task")["envelope"] == "task_run"
    assert _task(graph, "telemetry_task")["envelope"] == "task_failure_telemetry"
    assert _task(graph, "orphan_task")["registered"] is False
    assert _task(graph, "service_heavy_task")["body_lines"] == 5
    assert "on_task_done" not in {t["name"] for t in graph["nodes"]["tasks"]}


def test_each_task_belongs_to_the_instance_that_decorates_it(duo_graph: Any) -> None:
    # case DUO-instance
    instances = {t["name"]: t["instance"] for t in duo_graph["nodes"]["tasks"]}
    assert instances == {
        "lib_task": "evgduo.evg_duo_main_app:huey",
        "cue_task": "evgduo.evg_duo_cue_app:cue",
        "misrouted_task": "evgduo.evg_duo_cue_app:cue",
    }


def test_registered_means_imported_by_its_own_instances_app(duo_graph: Any) -> None:
    # case DUO-registered (misrouted_task's module is imported by the other app only)
    registered = {t["name"]: t["registered"] for t in duo_graph["nodes"]["tasks"]}
    assert registered == {"lib_task": True, "cue_task": True, "misrouted_task": False}
    unregistered = {
        h["subject"]
        for h in duo_graph["hits"]
        if h["code"] == "EV06" and h["detail"] == "unregistered_module"
    }
    assert unregistered == {"evgduo.tasks.evg_duo_misrouted_tasks"}
    assert not [h for h in duo_graph["hits"] if h["code"] == "EV05"]


def test_producer_layers(graph: Any) -> None:
    # case LAYER
    layers = {p["qualname"]: p["layer"] for p in graph["nodes"]["producers"]}
    assert layers[f"{API}:start_batch"] == "router"
    assert layers[f"{CHAIN}:chain_head_task"] == "task"
    assert layers["evgapp.services.evg_catalog_service:kick_tail"] == "service"
    assert layers[f"{BENCH}:bench"] == "script"
    assert layers["evgapp.evg_main:lifespan"] == "lifespan"
    assert layers["evgapp.evg_poller:watch_progress"] == "other"


def test_counts(graph: Any) -> None:
    # case TOTALS
    assert graph["counts"] == {
        "node.task": 16,
        "node.periodic": 3,
        "node.producer": 28,
        "edge.ENQUEUE": 23,
        "edge.ENQUEUE_DEFERRED": 1,
        "edge.INLINE": 3,
        "edge.PUBLISH": 5,
        "edge.SUBSCRIBE": 4,
        "edge.POLL": 1,
        "class.COMMAND": 23,
        "class.QUERY": 1,
        "class.EVENT": 5,
        "rule.EV01": 3,
        "rule.EV02": 3,
        "rule.EV03": 1,
        "rule.EV04": 1,
        "rule.EV05": 1,
        "rule.EV06": 3,
        "rule.EV07": 3,
        "rule.EV08": 19,
        "rule.EV09": 2,
        "rule.EV10": 23,
        "unresolved_dispatch": 5,
    }


# ---- output format and determinism ----


def test_two_runs_are_byte_identical(tmp_path: Path) -> None:
    # case DET-bytes
    out = tmp_path / "event-graph.json"
    _analyse(MAIN, tmp_path, *MAIN_OPTS)
    first = out.read_bytes()
    _analyse(MAIN, tmp_path, *MAIN_OPTS)
    assert out.read_bytes() == first


def test_relative_and_absolute_root_give_identical_output(tmp_path: Path) -> None:
    # case DET-root
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    rel = _run("main", tmp_path / "a" / "event-graph.json", *MAIN_OPTS, cwd=_FIXTURES)
    absolute = _run(MAIN.resolve(), tmp_path / "b" / "event-graph.json", *MAIN_OPTS)
    assert rel.returncode == 0, rel.stderr
    assert absolute.returncode == 0, absolute.stderr
    assert (tmp_path / "a" / "event-graph.json").read_bytes() == (
        tmp_path / "b" / "event-graph.json"
    ).read_bytes()


def test_output_is_sorted_relative_and_timeless(tmp_path: Path) -> None:
    # case DET-format
    out = tmp_path / "event-graph.json"
    _analyse(MAIN, tmp_path, *MAIN_OPTS)
    text = out.read_text(encoding="utf-8")
    data = json.loads(text)
    assert text == json.dumps(data, sort_keys=True, indent=2) + "\n"
    assert data["schema_version"] == 1
    assert "\\\\" not in text
    assert str(tmp_path) not in text
    assert str(MAIN.resolve()) not in text
    assert MAIN.resolve().as_posix() not in text
    edges = [(e["kind"], e["producer"], e["consumer"]) for e in data["edges"]]
    assert edges == sorted(edges)
    hits = [(h["code"], h["subject"]) for h in data["hits"]]
    assert hits == sorted(hits)
    tasks = [(t["module"], t["name"]) for t in data["nodes"]["tasks"]]
    assert tasks == sorted(tasks)


def test_header_records_every_option(graph: Any) -> None:
    # case HEADER
    options = graph["header"]["options"]
    assert options["root"] == "."
    assert options["out"] == "event-graph.json"
    assert options["guard_fn"] == GUARD
    assert options["publish_fn"] == ["evgapp.evg_bus:publish"]
    assert options["subscribe_fn"] == ["evgapp.evg_bus:subscribe"]
    assert options["ev01_scope"] == ["unguarded_head_task"]


def test_header_records_the_excluded_directories(graph: Any) -> None:
    # case HEADER-excluded
    assert graph["header"]["excluded_dirs"] == [
        ".*",
        "__pycache__",
        "frontend",
        "htmlcov",
        "node_modules",
        "tests",
    ]


# ---- --check ----


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the main tree with a baseline written beside it."""
    copy = tmp_path / "tree"
    shutil.copytree(MAIN, copy)
    proc = _run(copy, tmp_path / "baseline.json", *MAIN_OPTS)
    assert proc.returncode == 0, proc.stderr
    return copy


def _check(tree: Path) -> int:
    baseline = tree.parent / "baseline.json"
    proc = _run(tree, tree.parent / "current.json", *MAIN_OPTS, "--check", str(baseline))
    return proc.returncode


def test_check_unchanged_tree_is_clean(tree: Path) -> None:
    # case CHECK-same
    assert _check(tree) == 0


def test_check_ignores_a_line_only_move(tree: Path) -> None:
    # case CHECK-lineonly
    for rel in ("evgapp/tasks/evg_chain_tasks.py", "evgapp/routers/evg_api.py"):
        path = tree / rel
        path.write_text("\n\n\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert _check(tree) == 0


def test_check_fails_on_a_new_hit(tree: Path) -> None:
    # case CHECK-new
    path = tree / "evgapp/tasks/evg_chain_tasks.py"
    extra = "\n\n@huey.task()\ndef new_unguarded_task() -> None:\n    chain_tail_task()\n"
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    assert _check(tree) == 1


def test_check_is_clean_when_a_hit_goes_away(tree: Path) -> None:
    # case CHECK-removed
    (tree / "evgapp/tasks/evg_orphan_tasks.py").unlink()
    assert _check(tree) == 0


def test_check_fails_on_new_unresolved_dispatch(tree: Path) -> None:
    # case CHECK-unresolved (a getattr dispatch adds no hit, only an unresolved row)
    path = tree / "evgapp/routers/evg_api.py"
    extra = "\n\ndef dispatch_again(name: str) -> None:\n    getattr(evg_chain_tasks, name)()\n"
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")
    assert _check(tree) == 1


def test_missing_root_is_an_analysis_error(tmp_path: Path) -> None:
    # case EXIT-2
    proc = _run(tmp_path / "no-such-root", tmp_path / "out.json")
    assert proc.returncode == 2
    assert "no-such-root" in proc.stderr


# ---- analyzer source ----


def test_analyzer_source_names_no_fixture() -> None:
    # case SRC-nofixture
    source = _SCRIPT.read_text(encoding="utf-8")
    assert "fixtures" not in source
    assert "evg_" not in source
    assert "evgapp" not in source
    assert "evgalt" not in source
    assert "evgduo" not in source
    for path in _FIXTURES.rglob("*.py"):
        assert path.name not in source
        assert path.stem not in source


def test_analyzer_imports_stdlib_only() -> None:
    # case SRC-stdlib
    tree = ast.parse(_SCRIPT.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}
    assert not roots & {"socket", "sqlite3", "urllib", "http", "ssl", "ftplib", "smtplib"}


# ---- CLI listings ----


def test_summary_prints_the_counts_in_40_lines_or_fewer(tmp_path: Path) -> None:
    # case CLI-summary
    out = tmp_path / "event-graph.json"
    proc = _run(MAIN, out, *MAIN_OPTS, "--summary")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert len(lines) <= 40
    assert "no publish mechanism found" not in lines
    printed = {k: int(v) for k, _, v in (line.partition(": ") for line in lines) if v}
    assert printed == json.loads(out.read_text(encoding="utf-8"))["counts"]


def test_rule_listing_is_capped_and_pages(tmp_path: Path) -> None:
    # case CLI-rule
    out = tmp_path / "event-graph.json"
    first = _run(MAIN, out, *MAIN_OPTS, "--rule", "EV02", "--limit", "2")
    assert first.returncode == 0, first.stderr
    lines = first.stdout.splitlines()
    assert lines[-1] == "1 more (--offset 2)"
    assert f"{BENCH}:bench -> chain_tail_task" in first.stdout
    assert f"{BENCH}:bench_raw -> chain_tail_task" in first.stdout
    assert "resume_periodic" not in first.stdout
    second = _run(MAIN, out, *MAIN_OPTS, "--rule", "EV02", "--limit", "2", "--offset", "2")
    assert second.returncode == 0, second.stderr
    assert f"{MISC}:resume_periodic -> chain_tail_task" in second.stdout
    assert "evg_bench" not in second.stdout


def test_node_listing_shows_in_and_out_edges(tmp_path: Path) -> None:
    # case CLI-node
    proc = _run(MAIN, tmp_path / "event-graph.json", *MAIN_OPTS, "--node", "chain_mid_task")
    assert proc.returncode == 0, proc.stderr
    assert f"ENQUEUE {CHAIN}:chain_head_task -> chain_mid_task" in proc.stdout
    assert f"ENQUEUE {CHAIN}:chain_mid_task -> chain_tail_task" in proc.stdout


def test_chains_listing_shows_longest_paths_and_cycles(tmp_path: Path) -> None:
    # case CLI-chains
    proc = _run(MAIN, tmp_path / "event-graph.json", *MAIN_OPTS, "--chains")
    assert proc.returncode == 0, proc.stderr
    assert "chain_head_task -> chain_mid_task -> chain_tail_task" in proc.stdout
    assert "cyc_a_task -> cyc_b_task -> cyc_a_task" in proc.stdout


# ---- envelope: context managers that catch the task's failures (GAP-01) ----

_CM_APP = """\
import contextlib
from contextlib import contextmanager

from huey import SqliteHuey

huey = SqliteHuey(filename="cm.db", results=False)


@contextmanager
def reported_failures(name):
    try:
        yield
    except OSError:
        print(name)


@contextlib.contextmanager
def attr_reported_failures(name):
    try:
        yield
    except OSError:
        print(name)


@contextmanager
def timer(name):
    yield


@contextmanager
def task_run(name):
    yield


@huey.task()
def reported_task():
    with reported_failures("x"):
        work()


@huey.task()
def attribute_form_task():
    with attr_reported_failures("x"):
        work()


@huey.task()
def timer_task():
    with timer("x"):
        work()


@huey.task()
def timer_with_own_try_task():
    with timer("x"):
        try:
            work()
        except OSError:
            print("own")


@huey.task()
def task_run_wins_task():
    with reported_failures("x"), task_run("x"):
        work()


def work():
    return None
"""


@pytest.fixture(scope="module")
def cm_graph(tmp_path_factory: pytest.TempPathFactory) -> Any:
    root = tmp_path_factory.mktemp("cm_root")
    (root / "cm_app.py").write_text(_CM_APP, encoding="utf-8")
    return _analyse(root, tmp_path_factory.mktemp("cm_out"))


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("reported_task", "context"),  # GAP-01 a
        ("timer_task", "none"),  # GAP-01 b: a @contextmanager without try/handler
        ("timer_with_own_try_task", "own"),  # GAP-01 c
        ("task_run_wins_task", "task_run"),  # GAP-01 d
        ("attribute_form_task", "context"),  # GAP-01 e: @contextlib.contextmanager
    ],
)
def test_envelope_recognises_context_manager_envelopes(
    cm_graph: Any, task: str, expected: str
) -> None:
    # case GAP-01
    assert _task(cm_graph, task)["envelope"] == expected
