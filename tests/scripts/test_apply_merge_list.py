"""``scripts/apply_merge_list.py``: the merge list's Apply on a gate copy (artist model phase 3b
PR 4; SPEC S11, D26; scope Q20; the gate's ``--apply`` step; AUD-R080).

``apply_merge_list.py --dsn <copy> [--skip <file>]`` plans every group, as the merge list does,
and applies each one as Apply does: through the guard, reason "merge list", with no approvals,
committing after each group. A group whose merge would change what a pin can't hold waits, is
counted and left unmerged. The skip file names works (one id a row; blank rows and ``#``
comments ignored): a group holding any of them is skipped. It prints its counts.

It writes, to ``GATE_RUN_DIR``:

- ``work_map.json``: each merged-away work id -> the work it merged into, for the compares;
- ``merge_list_allow.txt``: the gate's allowlist of the differences the merges make, one
  tab-separated ``<section> <key> <reason>`` a row: ``work-source`` of every (station, line)
  whose line it pinned (``master`` -> ``pin``, the same file); ``masters``, ``overrides`` (each
  override row of the merged work) and the golden ``work-detail`` of each merged work and of
  each work merged into it (D35: a sampled merged-away id answers its target's detail); the
  golden ``artist-detail`` and ``artist-list`` entries of each artist the merged works credit.

A second run finds nothing to merge. Like every gate tool it refuses a database without "bench"
in its name before touching anything (S10), and like the list it waits for D1's credits (Q22).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from backend.domain.library import WORK_ARTISTS_CREDITED_SETTING
from tests.integration import credit_picks_seed as cp
from tests.integration import work_merge_seed as seed
from tests.integration.work_merge_seed import TwoWorks
from tests.scripts.gate_world import Conn, load_tool, read_json, with_dbname

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("process_env")]

ALLOWLIST = "merge_list_allow.txt"


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    return load_tool("apply_merge_list")


@pytest.fixture
def run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GATE_RUN_DIR", str(tmp_path))
    return tmp_path


def credits_ready(conn: Conn) -> None:
    cp.mark_built(conn)
    cp.set_marker(conn, WORK_ARTISTS_CREDITED_SETTING)


def allowed(run_dir: Path) -> set[tuple[str, str]]:
    gate_common = load_tool("gate_common")
    found = gate_common.read_allowlist(run_dir / ALLOWLIST)
    assert isinstance(found, dict), found
    return set(found)


class Copy:
    """Three groups on the copy: "Always" and "Runaway" merge (a source line is pinned in each),
    "Bed Of Roses" waits (the source's AC override would reach the target's line at an AC
    station)."""

    def __init__(self, conn: Conn) -> None:
        credits_ready(conn)
        self.at = seed.station(conn)
        self.always = TwoWorks(conn, "Always")
        self.always_line = seed.heard(conn, self.always.source_master, self.at, title="Always")
        seed.heard(conn, self.always.target_master, self.at, title="Always", times=3)
        self.runaway = TwoWorks(conn, "Runaway")
        self.runaway_line = seed.heard(conn, self.runaway.source_master, self.at, title="Runaway")
        seed.heard(conn, self.runaway.target_master, self.at, title="Runaway", times=2)
        self.roses = TwoWorks(conn, "Bed Of Roses")
        ac_edit = self.roses.source_file(conn)
        cp.override(conn, self.roses.source, "AC", ac_edit)
        self.roses_line = seed.heard(
            conn, self.roses.target_master, seed.station(conn, "AC"), title="Bed Of Roses"
        )
        conn.commit()


def test_applies_every_unflagged_group(
    tool: ModuleType, bench: str, conn: Conn, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = Copy(conn)

    assert tool.main(["--dsn", bench]) == 0

    assert not seed.work_exists(conn, world.always.source)
    assert not seed.work_exists(conn, world.runaway.source)
    assert seed.work_exists(conn, world.roses.source)
    assert seed.pin_of(conn, world.always_line) == world.always.source_master
    assert seed.playing(conn, world.always_line) == {(world.always.source_master, "pin")}
    assert [m["reason"] for m in seed.merges_of(conn, world.always.target)] == ["merge list"]
    assert "merged 2, waiting 1, failed 0, skipped 0" in capsys.readouterr().out


def test_skip_file(
    tool: ModuleType, bench: str, conn: Conn, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = Copy(conn)
    skip = run_dir / "skip.txt"
    skip.write_text(f"# Lance's skips\n\n{world.runaway.source}\n", encoding="utf-8")

    assert tool.main(["--dsn", bench, "--skip", str(skip)]) == 0

    assert not seed.work_exists(conn, world.always.source)
    assert seed.work_exists(conn, world.runaway.source)
    assert seed.pin_of(conn, world.runaway_line) is None
    assert "merged 1, waiting 1, failed 0, skipped 1" in capsys.readouterr().out


def test_writes_the_work_map(tool: ModuleType, bench: str, conn: Conn, run_dir: Path) -> None:
    world = Copy(conn)

    tool.main(["--dsn", bench])

    assert read_json(run_dir / "work_map.json") == {
        world.always.source: world.always.target,
        world.runaway.source: world.runaway.target,
    }


def test_writes_the_allowlist(tool: ModuleType, bench: str, conn: Conn, run_dir: Path) -> None:
    """One group: both works override CHR (no CHR station, so nothing that plays changes); the
    target's wins, so two override rows land on one key. The source's line is pinned."""
    credits_ready(conn)
    at = seed.station(conn)
    world = TwoWorks(conn, "Always")
    line = seed.heard(conn, world.source_master, at, title="Always")
    seed.heard(conn, world.target_master, at, title="Always", times=2)
    cp.override(conn, world.target, "CHR", world.target_master)
    cp.override(conn, world.source, "CHR", world.source_master)
    conn.commit()

    assert tool.main(["--dsn", bench]) == 0

    assert seed.pin_of(conn, line) == world.source_master
    assert allowed(run_dir) == {
        ("work-source", f"{at}/{line}"),
        ("masters", world.target),
        ("overrides", f"{world.target}/format_name=CHR"),
        ("work-detail", world.target),
        ("work-detail", world.source),
        ("artist-detail", world.act),
        ("artist-list", world.act),
    }


def test_flagged_groups_are_left_and_counted(
    tool: ModuleType, bench: str, conn: Conn, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = Copy(conn)

    tool.main(["--dsn", bench])

    assert seed.work_exists(conn, world.roses.source)
    assert seed.merges_of(conn, world.roses.target) == []
    assert world.roses.source not in read_json(run_dir / "work_map.json")
    assert not {key for _, key in allowed(run_dir)} & {
        world.roses.target,
        world.roses.source,
        str(world.roses_line),
    }
    assert "waiting 1" in capsys.readouterr().out


def test_idempotent(
    tool: ModuleType, bench: str, conn: Conn, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = Copy(conn)
    tool.main(["--dsn", bench])
    conn.commit()
    before = seed.snapshot(conn)
    capsys.readouterr()

    assert tool.main(["--dsn", bench]) == 0

    conn.commit()
    assert seed.snapshot(conn) == before
    assert "merged 0, waiting 1" in capsys.readouterr().out
    assert seed.redirects(conn, [world.always.source]) == {world.always.source: world.always.target}


def test_it_waits_for_d1s_credits(
    tool: ModuleType, bench: str, conn: Conn, run_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q22: with no D1 marker on the copy, nothing is planned or merged."""
    cp.mark_built(conn)
    world = TwoWorks(conn, "Always")
    conn.commit()

    assert tool.main(["--dsn", bench]) != 0

    assert seed.work_exists(conn, world.source)
    assert "credits" in capsys.readouterr().out.lower()


def test_it_refuses_a_database_without_bench(tool: ModuleType, bench: str, conn: Conn) -> None:
    """S10: named without "bench", the run ends before it connects."""
    world = Copy(conn)
    not_a_copy = with_dbname(bench, "retrostation_gate_decoy_missing")

    with pytest.raises(SystemExit) as refused:
        tool.main(["--dsn", not_a_copy])

    assert refused.value.code not in (0, None)
    assert seed.work_exists(conn, world.always.source)
