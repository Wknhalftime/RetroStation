# The one copy of the autocue settings (spec: Cue pre-computation: autocue.internal,
# lufs_target -18, amplify_behavior "keep"; D69: timeout 30 s, so autocue measures songs
# up to about 35 min).
# cue_analysis.liq includes it, and PR E2 includes it in session.liq, so the batch analyser
# and the engine cue a file alike. A change here is an analyser change: bump
# CUE_ANALYSER_VERSION so the rows it made are purged (D20).
settings.autocue.internal.lufs_target := -18.
settings.autocue.amplify_behavior := "keep"
settings.autocue.internal.timeout := 30.
