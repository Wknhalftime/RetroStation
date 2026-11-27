"""The linker's and song matching's thresholds, built in one function (artist model phase 4.2
PR 1, SPEC S3; design C25, C48; AUD-R022 D6, AUD-R095).

``build_thresholds(settings)`` in ``repository_factory`` is the one place that builds
``LinkingThresholds`` and ``SongThresholds``: nothing else in ``backend/`` or ``scripts/``
constructs either. The song values are unchanged (AUD-R022 D6, as 4.3's SPEC states them):
auto-match at 95, or with a competitor at 80 and a 10-point lead, and a floor of 56. Each comes
from where song matching reads it today, so only the 80 follows a setting. It reads only the
seven settings the locked matching-run rigs carry (``test_matching_run_stages.py``,
``test_task_handoffs_r014.py``), so a task that builds them never needs a relock. No linking
value is pinned here: D1 sets ``title_lead`` from D4's count, and the sample sets the rest.

The new names are imported inside the cases, so the one-site scan fails on its own assert.
"""

from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
BUILDERS = {"LinkingThresholds", "SongThresholds"}


def rig_settings(**overrides: object) -> SimpleNamespace:
    """The seven fields the locked rigs' settings hold, at today's config defaults."""
    fields: dict[str, object] = {
        "database_url": "postgresql://unused/unused",
        "mb_cache_ttl_days": 3650,
        "strong_match_threshold": 80,
        "mb_score_gap": 10,
        "mb_auto_link_score": 95,
        "min_presentation_score": 56,
        "broadcast_name_max_len": 30,
    }
    return SimpleNamespace(**{**fields, **overrides})


def _calls(node: ast.AST, enclosing: str) -> set[tuple[str, str]]:
    """(enclosing function, class built) for each call under *node*; "<module>" outside any
    function, so a module-level default counts as a site too."""
    found: set[tuple[str, str]] = set()
    for child in ast.iter_child_nodes(node):
        inner = (
            child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else enclosing
        )
        call = child.func if isinstance(child, ast.Call) else None
        name = call.id if isinstance(call, ast.Name) else getattr(call, "attr", None)
        if name in BUILDERS:
            found.add((enclosing, str(name)))
        found |= _calls(child, inner)
    return found


def _construction_sites() -> set[tuple[str, str, str]]:
    """(file, enclosing function, class built) for every call that builds either type."""
    sites: set[tuple[str, str, str]] = set()
    for folder in ("backend", "scripts"):
        for path in (ROOT / folder).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            relative = path.relative_to(ROOT).as_posix()
            sites |= {(relative, func, built) for func, built in _calls(tree, "<module>")}
    return sites


def test_both_are_built_in_one_function_in_repository_factory() -> None:
    assert _construction_sites() == {
        ("backend/services/repository_factory.py", "build_thresholds", "LinkingThresholds"),
        ("backend/services/repository_factory.py", "build_thresholds", "SongThresholds"),
    }


def test_the_song_values_are_unchanged() -> None:
    """AUD-R022 D6: 95; 80 with a 10-point lead; the floor of 56, each read where song matching
    reads it today: the 95, the lead and the floor from its constants
    (identity_matching_service.py's MB_AUTO_LINK_SCORE, MB_SCORE_GAP and
    SONG_MIN_PRESENTATION_SCORE), and only the 80 from the settings (identity_matching_tasks.py,
    strong_match_threshold)."""
    from backend.config import Settings
    from backend.domain.matching import SongThresholds
    from backend.services.matching_constants import (
        MB_AUTO_LINK_SCORE,
        MB_SCORE_GAP,
        SONG_MIN_PRESENTATION_SCORE,
    )
    from backend.services.repository_factory import build_thresholds

    song = build_thresholds(rig_settings()).song

    assert song == SongThresholds(auto_match=95, strong_match=80, lead=10, floor=56)
    assert (song.auto_match, song.lead, song.floor) == (
        MB_AUTO_LINK_SCORE,
        MB_SCORE_GAP,
        SONG_MIN_PRESENTATION_SCORE,
    )
    assert Settings.model_fields["strong_match_threshold"].default == 80


def test_only_the_strong_match_follows_the_settings() -> None:
    """mb_auto_link_score and mb_score_gap feed only the artist stage, and C25 retires them;
    an artist-tuned .env never moves song matching (C48)."""
    from backend.services.repository_factory import build_thresholds

    song = build_thresholds(
        rig_settings(mb_auto_link_score=97, strong_match_threshold=81, mb_score_gap=12)
    ).song

    assert (song.auto_match, song.strong_match, song.lead) == (95, 81, 10)


def test_the_seven_fields_of_the_locked_rigs_are_enough() -> None:
    """R-e: a task module building them reads nothing the rigs' settings lack."""
    from backend.services.repository_factory import build_thresholds

    thresholds = build_thresholds(rig_settings(broadcast_name_max_len=24))

    assert thresholds.linking.name_max_len == 24


def test_the_linking_values_are_their_modules_defaults() -> None:
    """C25: one frozen object, defaults in one module; only the name field's width comes from
    the settings. The design fixes two defaults: a top-10 shortlist (C32) and common at 3
    distinct acts (C8). title_lead and near_miss stay free (D1 and the sample tune them)."""
    from backend.domain.linking import LinkingThresholds
    from backend.services.repository_factory import build_thresholds

    linking = build_thresholds(rig_settings()).linking

    assert linking == replace(LinkingThresholds(), name_max_len=30)
    assert (linking.shortlist, linking.common_acts) == (10, 3)
    assert build_thresholds(rig_settings()) == build_thresholds(rig_settings())


def test_both_are_frozen() -> None:
    from backend.services.repository_factory import build_thresholds

    thresholds = build_thresholds(rig_settings())

    with pytest.raises(FrozenInstanceError):
        thresholds.song.floor = 1  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        thresholds.linking.title_lead = 9  # type: ignore[misc]
