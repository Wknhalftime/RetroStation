"""Single source of truth for matcher scoring thresholds.

All strategies and the router's triage computation import from here. Never
redeclare any of these values elsewhere; never copy-paste their literals into
inline checks.
"""

MB_AUTO_LINK_SCORE: int = 95
MB_SCORE_GAP: int = 10
# Floor for MusicBrainz search results considered as match candidates at all.
# Below this score, results are treated as noise and discarded before zoning.
MB_MIN_CANDIDATE_SCORE: int = 60
MID_BAND_LOWER: int = 55
MID_BAND_UPPER: int = 64
MID_BAND_GAP_THRESHOLD: int = 5
MIN_PRESENTATION_SCORE: int = 50
# Song matching only (AUD-R022 D6). A song whose best candidate scores under this (and does not
# auto-match) is auto_rejected; the router's song triage starts "needs_attention" here. Songs
# have no mid-band (D12): MID_BAND_* and MIN_PRESENTATION_SCORE are artist matching's.
SONG_MIN_PRESENTATION_SCORE: int = 56
# V11.1's strong-match score in trial mode (AUD-R100), in place of strong_match_threshold: with
# an MB_SCORE_GAP lead over a real competitor, V11.1 auto-matches a song from here.
V11_STRONG_MATCH_SCORE: int = 85
# Song name lookups (Tier 1 Step C, Tier 2): a bound, not a sample (D13). Dev DB max 177 files.
SONG_NAME_LOOKUP_LIMIT: int = 1000
# Song matching's MusicBrainz cut-off (AUD-R028, D16): after this many consecutive transient
# recording-search failures, one run stops calling MusicBrainz and defers the songs that need it.
SONG_MB_FAILURE_CUTOFF: int = 5
# Triage boundary: identities with confidence ≥ this render as "quick_review".
# Below this (but ≥ SONG_MIN_PRESENTATION_SCORE) they render as "needs_attention".
QUICK_REVIEW_MIN_SCORE: int = 65
