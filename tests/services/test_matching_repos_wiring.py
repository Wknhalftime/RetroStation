"""The matching tasks pass the link port (artist model phase 1a, SPEC S7; C30, AUD-R031's
precedent for ``recording_search_reach``).

``IdentityMatchingRepos`` (Tier 1's link reader) and ``ArtistMatchingRepos`` (the matcher's
link writer) gain ``link_repo``, an optional field defaulting to None, which means "no links":
locked tests that build these dataclasses without it keep working. So that None never reaches
production, every place in ``backend/`` and ``scripts/`` that builds one passes ``link_repo``
explicitly, and not as None; the two tasks and the gate's dry run are among them.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path

from backend.services.artist_matching_service import ArtistMatchingRepos
from backend.services.identity_matching_service import IdentityMatchingRepos

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("backend", "scripts")
REPOS = {"IdentityMatchingRepos", "ArtistMatchingRepos"}
MUST_BUILD = {
    ("backend/tasks/identity_matching_tasks.py", "IdentityMatchingRepos"),
    ("backend/tasks/artist_matching_tasks.py", "ArtistMatchingRepos"),
    ("scripts/dry_run_matching.py", "IdentityMatchingRepos"),
    ("scripts/dry_run_matching.py", "ArtistMatchingRepos"),
}


def _builds(source: str) -> Iterator[tuple[str, int, ast.Call]]:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in REPOS:
            yield str(name), node.lineno, node


def _passes_a_link_port(call: ast.Call) -> bool:
    link = next((k.value for k in call.keywords if k.arg == "link_repo"), None)
    return link is not None and not (isinstance(link, ast.Constant) and link.value is None)


def _every_build() -> dict[tuple[str, str, int], bool]:
    found: dict[tuple[str, str, int], bool] = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            for name, line, call in _builds(path.read_text(encoding="utf-8")):
                found[relative, name, line] = _passes_a_link_port(call)
    return found


def test_link_repo_is_an_optional_field_defaulting_to_no_links() -> None:
    for repos in (IdentityMatchingRepos, ArtistMatchingRepos):
        fields = {f.name: f for f in dataclasses.fields(repos)}
        assert "link_repo" in fields, repos.__name__
        assert fields["link_repo"].default is None, repos.__name__


def test_the_tasks_always_pass_the_link_port() -> None:
    builds = _every_build()

    assert {(path, name) for path, name, _ in builds} >= MUST_BUILD
    assert sorted(key for key, passes in builds.items() if not passes) == []


def test_the_scan_reads_a_build() -> None:
    passes = "IdentityMatchingRepos(match_repo=m, link_repo=PgLogNameLinkRepository(conn))"
    fails = [
        "IdentityMatchingRepos(match_repo=m)",
        "service.ArtistMatchingRepos(match_repo=m, link_repo=None)",
    ]

    assert [_passes_a_link_port(call) for _, _, call in _builds(passes)] == [True]
    assert [_passes_a_link_port(call) for s in fails for _, _, call in _builds(s)] == [
        False,
        False,
    ]
