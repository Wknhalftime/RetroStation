"""The matcher and the artist Resolve are given the not-an-artist list (artist model phase 1c,
SPEC S6, E3; C7, the precedent of ``test_matching_repos_wiring.py``).

``ArtistMatchingRepos`` (the matcher, which never matches a listed name) and
``ArtistResolutionRepos`` (Resolve, which never links one) gain ``placeholders``, the
``LogNamePlaceholderRepository``: an optional field defaulting to None, which means "nothing
is listed", so the locked tests that build these dataclasses without it keep working. So that
None never reaches production, every place in ``backend/`` and ``scripts/`` that builds one
passes ``placeholders`` explicitly, and not as None: the artist matching task, the gate's dry
run (so it decides what the task would) and the request's Resolve bundle among them.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path

from backend.services.artist_matching_service import ArtistMatchingRepos
from backend.services.artist_resolution_service import ArtistResolutionRepos

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("backend", "scripts")
REPOS = {"ArtistMatchingRepos", "ArtistResolutionRepos"}
MUST_BUILD = {
    ("backend/tasks/artist_matching_tasks.py", "ArtistMatchingRepos"),
    ("scripts/dry_run_matching.py", "ArtistMatchingRepos"),
    ("backend/services/repository_factory.py", "ArtistResolutionRepos"),
}


def _builds(source: str) -> Iterator[tuple[str, int, ast.Call]]:
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in REPOS:
            yield str(name), node.lineno, node


def _passes_the_list(call: ast.Call) -> bool:
    given = next((k.value for k in call.keywords if k.arg == "placeholders"), None)
    return given is not None and not (isinstance(given, ast.Constant) and given.value is None)


def _every_build() -> dict[tuple[str, str, int], bool]:
    found: dict[tuple[str, str, int], bool] = {}
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(ROOT).as_posix()
            for name, line, call in _builds(path.read_text(encoding="utf-8")):
                found[relative, name, line] = _passes_the_list(call)
    return found


def test_placeholders_is_an_optional_field_defaulting_to_nothing_listed() -> None:
    for repos in (ArtistMatchingRepos, ArtistResolutionRepos):
        fields = {f.name: f for f in dataclasses.fields(repos)}
        assert "placeholders" in fields, repos.__name__
        assert fields["placeholders"].default is None, repos.__name__


def test_production_always_passes_the_list() -> None:
    builds = _every_build()

    assert {(path, name) for path, name, _ in builds} >= MUST_BUILD
    assert sorted(key for key, passes in builds.items() if not passes) == []


def test_the_scan_reads_a_build() -> None:
    passes = "ArtistMatchingRepos(rules_repo=r, placeholders=factory.log_name_placeholders)"
    fails = [
        "ArtistMatchingRepos(rules_repo=r)",
        "service.ArtistResolutionRepos(links=l, placeholders=None)",
    ]

    assert [_passes_the_list(call) for _, _, call in _builds(passes)] == [True]
    assert [_passes_the_list(call) for src in fails for _, _, call in _builds(src)] == [
        False,
        False,
    ]
