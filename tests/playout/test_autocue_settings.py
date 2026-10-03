"""One copy of the autocue settings (audit: "one cue write path shared by the Huey task and
the engine POST ... either the autocue settings are identical, or each writer gets a distinct
analyser_version"; spec: Cue pre-computation names the settings). The live tests prove the
settings take effect; this pins that there is one copy of them."""

from __future__ import annotations

from backend.playout.cue_analysis import ANALYSER_SCRIPT, AUTOCUE_SETTINGS


def autocue_lines(text: str) -> set[str]:
    return {
        line.strip() for line in text.splitlines() if line.strip().startswith("settings.autocue")
    }


def test_the_shared_autocue_settings_are_the_specs() -> None:
    """Cue pre-computation: lufs_target -18, amplify_behavior "keep"; D69: timeout 30 s."""
    assert autocue_lines(AUTOCUE_SETTINGS.read_text(encoding="utf-8")) == {
        "settings.autocue.internal.lufs_target := -18.",
        'settings.autocue.amplify_behavior := "keep"',
        "settings.autocue.internal.timeout := 30.",
    }


def test_the_analyser_takes_the_shared_settings_and_sets_none_of_its_own() -> None:
    """One copy: the batch script includes the shared file and adds no autocue setting."""
    text = ANALYSER_SCRIPT.read_text(encoding="utf-8")
    assert f'%include "{AUTOCUE_SETTINGS.name}"' in text
    assert autocue_lines(text) == set()
