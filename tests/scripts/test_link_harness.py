"""The link harness (artist model phase 4.2 PR 1, SPEC S6, S8, E4, E5; D3; design C7, C12, C23,
C51; AUD-R043, AUD-R095).

``scripts/link_harness.py`` (``poe link-harness``) runs the production ``decide()`` offline on
frozen fixtures, one JSON file per name, read from the fixtures folder and every folder under it
(PR 2 keeps ``named/`` beside the queue and the sample), and writes a dated report. A fixture
with ``expected`` decisions is a named case (C51): checked, never counted. Every other fixture
is one name of the review queue: its plays count toward coverage unless it is a placeholder
(C7), and it is "linked" when ``decide()`` links any performer. A fixture with a ``sample``
("random", or "sweep_made" for an artist 2a's sweep made, D3) is in the precision sample: each
automatic link ``decide()`` makes on it is right, wrong or unlabelled by its ``labels``.

S8's gate passes with at most 5 wrong links, none unlabelled, coverage of at least 70% of the
queue's plays (C12), and a random sample of at least 200 names (AUD-R043's "on a sample of 200":
the lock request's ask; counted in names, so a stricter threshold that drops links stays free).
A sweep-made link marked wrong counts toward the 5 (D3). The report gives both numbers, the
automatic links by source, and the wrong links with their sources (E4). A threshold change
re-runs on the same fixtures (E5): no relabelling, unless a sampled name now links a performer
nobody labelled.
"""

from __future__ import annotations

import importlib
import json
import sys
import tomllib
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from backend.domain.linking import LinkingThresholds

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
NAMED = ROOT / "tests" / "fixtures" / "linking" / "named"
DAY = date(2026, 11, 27)
T = LinkingThresholds(title_lead=1, common_acts=3, shortlist=10, near_miss=0.85, name_max_len=30)


@pytest.fixture(scope="module")
def harness() -> ModuleType:
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("link_harness")


@pytest.fixture
def fixtures(tmp_path: Path) -> Path:
    """The fixtures folder; reports go to tmp_path / "out", outside it."""
    folder = tmp_path / "fixtures"
    folder.mkdir()
    return folder


def _evidence(key: str | None, acts: int = 1) -> dict[str, Any]:
    """A name the anchor links to *key* (one played title held by *acts* acts), or, for None,
    a name nothing links."""
    candidates = (
        []
        if key is None
        else [
            {
                "performers": [{"key": key, "name": key}],
                "fit": {
                    "explained": [0],
                    "exact": False,
                    "fuzzy": False,
                    "leftovers": [],
                    "bystanders": [],
                },
                "library_titles": [{"title": "t", "acts": acts}],
            }
        ]
    )
    return {
        "words": ["w"],
        "manual": False,
        "waiting": False,
        "candidates": candidates,
        "musicbrainz": [],
    }


def write(
    folder: Path,
    slug: str,
    *,
    plays: int,
    links: str | None,
    acts: int = 1,
    sample: str | None = None,
    labels: dict[str, str] | None = None,
    placeholder: bool = False,
) -> None:
    doc = {
        "name": slug.upper(),
        "why": [],
        "plays": plays,
        "placeholder": placeholder,
        "sample": sample,
        "labels": labels or {},
        "evidence": _evidence(links, acts),
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{slug}.json").write_text(json.dumps(doc), encoding="utf-8")


def sampled(folder: Path, n: int, wrong: int, *, kind: str = "random") -> None:
    """*n* sampled names of 1 play each, each one automatic link; the first *wrong* of them
    labelled wrong."""
    for i in range(n):
        key = f"mbid:{kind}-{i}"
        label = "wrong" if i < wrong else "right"
        write(folder, f"{kind}-{i}", plays=1, links=key, sample=kind, labels={key: label})


def run(harness: ModuleType, fixtures: Path, out: Path, thresholds: LinkingThresholds = T) -> Any:
    return harness.run(fixtures, out, thresholds, DAY)


def test_the_report_is_written_with_its_date(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    write(fixtures, "one", plays=10, links="mbid:one")

    report = run(harness, fixtures, tmp_path / "out")

    written = json.loads((tmp_path / "out" / "2026-11-27.json").read_text(encoding="utf-8"))
    assert written == report
    assert report["date"] == "2026-11-27"
    for key in ("precision", "coverage", "sources", "wrong_links", "named", "gate"):
        assert key in report, key


def test_fixtures_are_read_from_every_folder_under_the_fixtures_folder(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    write(fixtures / "queue", "linked", plays=60, links="mbid:linked")
    write(fixtures / "sample", "random-0", plays=40, links="mbid:r", sample="random")

    report = run(harness, fixtures, tmp_path / "out")

    assert report["coverage"]["queue_plays"] == 100
    assert report["precision"]["sampled_links"] == 1


def test_coverage_counts_plays_and_leaves_out_placeholders(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    """C12, C7: SPOT BREAK alone holds half the queue's plays; it isn't counted at all."""
    write(fixtures, "linked", plays=70, links="mbid:linked")
    write(fixtures, "unlinked", plays=30, links=None)
    write(fixtures, "spot-break", plays=1000, links=None, placeholder=True)

    coverage = run(harness, fixtures, tmp_path / "out")["coverage"]

    assert (coverage["linked_plays"], coverage["queue_plays"]) == (70, 100)
    assert coverage["share"] == pytest.approx(0.70)


def test_precision_counts_the_sampled_links_by_their_labels(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    sampled(fixtures, 10, wrong=2)

    precision = run(harness, fixtures, tmp_path / "out")["precision"]

    assert precision == {"sampled_links": 10, "wrong": 2, "unlabelled": 0}


@pytest.mark.parametrize(
    ("wrong", "linked", "passed"),
    [
        pytest.param(5, 70, True, id="5-wrong-and-70-percent-pass"),
        pytest.param(6, 70, False, id="6-wrong-fail"),
        pytest.param(5, 69, False, id="69-percent-fails"),
    ],
)
def test_gate(
    harness: ModuleType,
    fixtures: Path,
    tmp_path: Path,
    wrong: int,
    linked: int,
    passed: bool,
) -> None:
    """S8, C12: at most 5 wrong in 200, and coverage of at least 70%; both numbers shown."""
    sampled(fixtures, 200, wrong=wrong)  # 200 linked names of 1 play each
    write(fixtures, "linked", plays=linked * 1000 - 200, links="mbid:linked")
    write(fixtures, "unlinked", plays=(100 - linked) * 1000, links=None)

    report = run(harness, fixtures, tmp_path / "out")

    assert report["gate"]["passed"] is passed
    assert report["precision"]["wrong"] == wrong
    assert report["coverage"]["share"] == pytest.approx(linked / 100)
    assert (report["gate"]["max_wrong"], report["gate"]["min_coverage"]) == (5, 0.70)


def test_the_gate_needs_a_random_sample_of_200_names(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    """AUD-R043: "on a sample of 200" (the lock request's ask). With 199 sampled names the gate
    can't pass, however right they are; a name in the sample counts whether or not it links."""
    sampled(fixtures, 199, wrong=0)
    short = run(harness, fixtures, tmp_path / "short")
    write(fixtures, "random-199", plays=1, links=None, sample="random")
    full = run(harness, fixtures, tmp_path / "full")

    assert (short["gate"]["passed"], full["gate"]["passed"]) == (False, True)
    assert (full["gate"]["min_sample"], full["precision"]["sampled_links"]) == (200, 199)


def test_a_wrong_sweep_made_link_counts_toward_the_five(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    """D3 (counting is the cautious reading; the lock request's first ask names the ruling):
    5 wrong in the random 200 and 1 wrong sweep-made link fail the gate."""
    sampled(fixtures, 200, wrong=5)
    sampled(fixtures, 3, wrong=1, kind="sweep_made")
    write(fixtures, "linked", plays=10_000_000, links="mbid:linked")

    report = run(harness, fixtures, tmp_path / "out")

    assert (report["precision"]["wrong"], report["gate"]["passed"]) == (6, False)


def test_the_wrong_links_are_listed_with_their_sources(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    """E4: the report lists each wrong link by source, for tuning."""
    sampled(fixtures, 2, wrong=1)

    report = run(harness, fixtures, tmp_path / "out")

    assert report["wrong_links"] == [
        {"name": "RANDOM-0", "performer": "mbid:random-0", "signals": ["anchor"]}
    ]
    assert report["sources"] == {"anchor": 2}


def test_threshold_change(harness: ModuleType, fixtures: Path, tmp_path: Path) -> None:
    """E5: a new threshold re-runs on the frozen fixtures. A link that goes away needs no
    label; a sampled name that now links a performer nobody labelled is unlabelled, and the
    gate can't pass until it is labelled."""
    write(
        fixtures,
        "kept",
        plays=50,
        links="mbid:kept",
        sample="random",
        labels={"mbid:kept": "right"},
    )
    write(fixtures, "fringe", plays=50, links="mbid:fringe", acts=3, sample="random")

    stricter = run(harness, fixtures, tmp_path / "strict")
    looser = run(harness, fixtures, tmp_path / "loose", replace(T, common_acts=4))

    assert stricter["precision"] == {"sampled_links": 1, "wrong": 0, "unlabelled": 0}
    assert looser["precision"] == {"sampled_links": 2, "wrong": 0, "unlabelled": 1}
    assert looser["coverage"]["share"] == pytest.approx(1.0)
    assert looser["gate"]["passed"] is False


def test_every_named_case_holds(harness: ModuleType, tmp_path: Path) -> None:
    """C51: CI runs the named cases through the harness's own loader; they count nowhere."""
    report = run(harness, NAMED, tmp_path / "out")

    assert report["named"] == {"passed": len(list(NAMED.glob("*.json"))), "failed": []}
    assert report["coverage"]["queue_plays"] == 0
    assert report["precision"]["sampled_links"] == 0


def test_a_bad_fixture_names_its_file_and_field(
    harness: ModuleType, fixtures: Path, tmp_path: Path
) -> None:
    write(fixtures, "broken", plays=1, links="mbid:x")
    doc = json.loads((fixtures / "broken.json").read_text(encoding="utf-8"))
    del doc["evidence"]["words"]
    (fixtures / "broken.json").write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ValueError, match=r"broken\.json.*words"):
        run(harness, fixtures, tmp_path / "out")


def test_main_exits_0_on_a_pass_and_1_on_a_fail(harness: ModuleType, tmp_path: Path) -> None:
    """A passing run has the gate's sample: 200 sampled names, labelled right."""
    passing, failing = tmp_path / "pass", tmp_path / "fail"
    sampled(passing, 200, wrong=0)
    sampled(failing, 200, wrong=6)
    args = ["--out", str(tmp_path / "out"), "--date", "2026-11-27"]

    assert harness.main(["--fixtures", str(passing), *args]) == 0
    assert harness.main(["--fixtures", str(failing), *args]) == 1


def test_poe_link_harness_runs_the_script() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    task = config["tool"]["poe"]["tasks"]["link-harness"]
    command = task if isinstance(task, str) else task["cmd"]

    assert "scripts/link_harness.py" in command
