"""``scripts/apply_credit_picks.py``: Credit picks' Apply on a gate copy (performances phase 3a
PR 3, SPEC S9; the gate's ``--apply`` step, DoD).

``apply_credit_picks.py --dsn <copy> [--skip <file>]`` does what Apply does for every undecided
``credit`` proposal on the copy, through ``SongMatchService``: it stores the performance on the
line's best match (giving it a preferred copy when it has none) and marks the row ``applied``.
The skip file names lines (one ``track_identities`` id a row; blank rows and ``#`` comments are
ignored); their rows are marked ``skipped``, as Lance's ticks are. Version lines, ties, decided
rows and stale rows (by Apply's rule) are left as they are; a second run applies nothing. Like
every gate tool it refuses a database without "bench" in its name before touching anything
(S10). It prints how many it applied, skipped and found stale.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from tests.integration import credit_picks_seed as seed
from tests.integration.credit_picks_seed import Duet
from tests.scripts.gate_world import Conn, load_tool, with_dbname

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("process_env")]


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    return load_tool("apply_credit_picks")


class Proposed(Duet):
    """Three duet lines proposed for Apply and a live duet line listed apart."""

    def __init__(self, conn: Conn) -> None:
        super().__init__(conn)
        self.lines = [self.duet_line(conn) for _ in range(3)]
        self.ids = [seed.propose(conn, line, self.duet) for line in self.lines]
        self.live_line = self.duet_line(conn, "Who Says You Can't Go Home (Live)")
        self.live_id = seed.propose(
            conn, self.live_line, None, kind="version_line", candidates=[self.duet]
        )
        conn.commit()


def states(conn: Conn) -> dict[object, str]:
    conn.commit()
    return {r["identity_id"]: r["state"] for r in seed.proposals(conn)}


def test_applies_every_credit_row_but_the_skipped_lines(
    tool: ModuleType, bench: str, conn: Conn, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = Proposed(conn)
    skip = tmp_path / "skip.txt"
    skip.write_text(f"# Lance's skips\n\n{world.lines[1]}\n", encoding="utf-8")

    code = tool.main(["--dsn", bench, "--skip", str(skip)])

    assert code == 0
    assert states(conn) == {
        world.lines[0]: "applied",
        world.lines[1]: "skipped",
        world.lines[2]: "applied",
        world.live_line: "proposed",
    }
    assert [seed.stored(conn, line) for line in world.lines] == [
        {world.duet},
        {None},
        {world.duet},
    ]
    assert seed.stored(conn, world.live_line) == {None}
    assert seed.preferred_of(conn, world.duet) == world.duet_flac
    assert seed.resolved(conn, world.lines[0]) == {(world.duet_flac, "performance")}
    out = capsys.readouterr().out
    assert "applied 2" in out and "skipped 1" in out and "stale 0" in out


def test_with_no_skip_file_every_credit_row_is_applied(
    tool: ModuleType, bench: str, conn: Conn
) -> None:
    world = Proposed(conn)

    assert tool.main(["--dsn", bench]) == 0

    assert [states(conn)[line] for line in world.lines] == ["applied"] * 3
    assert states(conn)[world.live_line] == "proposed"


def test_a_second_run_applies_nothing(
    tool: ModuleType, bench: str, conn: Conn, capsys: pytest.CaptureFixture[str]
) -> None:
    world = Proposed(conn)
    tool.main(["--dsn", bench])
    first = states(conn)
    capsys.readouterr()

    assert tool.main(["--dsn", bench]) == 0

    assert states(conn) == first
    assert [seed.stored(conn, line) for line in world.lines] == [{world.duet}] * 3
    assert "applied 0" in capsys.readouterr().out


def test_a_stale_row_is_left_proposed(tool: ModuleType, bench: str, conn: Conn) -> None:
    """The duet became the default: storing it would store the default (C71)."""
    world = Proposed(conn)
    conn.execute(
        "UPDATE song_masters SET preferred_file_id = %s WHERE work_id = %s",
        (world.duet_mp3, world.work),
    )
    conn.commit()

    assert tool.main(["--dsn", bench]) == 0

    assert [states(conn)[line] for line in world.lines] == ["proposed"] * 3
    assert [seed.stored(conn, line) for line in world.lines] == [{None}] * 3


def test_it_refuses_a_database_without_bench(tool: ModuleType, bench: str, conn: Conn) -> None:
    """S10: named without "bench", the run ends before it connects."""
    world = Proposed(conn)
    not_a_copy = with_dbname(bench, "retrostation_gate_decoy_missing")

    with pytest.raises(SystemExit) as refused:
        tool.main(["--dsn", not_a_copy])

    assert refused.value.code not in (0, None)
    assert states(conn)[world.lines[0]] == "proposed"
