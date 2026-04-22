"""Single source of truth for matcher scoring thresholds.

All strategies and the router's triage computation import from here. Never
redeclare any of these values elsewhere; never copy-paste their literals into
inline checks. See `docs/superpowers/plans/2026-04-22-resolution-center-visibility.md`
§Task 1 for rationale and the zones defined in §3.3 of the source spec.
"""

MB_AUTO_LINK_SCORE: int = 95
MB_SCORE_GAP: int = 10
MID_BAND_LOWER: int = 55
MID_BAND_UPPER: int = 64
MID_BAND_GAP_THRESHOLD: int = 5
MIN_PRESENTATION_SCORE: int = 50
# Triage boundary: identities with confidence ≥ this render as "quick_review".
# Below this (but ≥ MIN_PRESENTATION_SCORE) they render as "needs_attention".
QUICK_REVIEW_MIN_SCORE: int = 65
