# -w 1 on both Huey consumers is a constraint, not a tuning value (AUD-R017).
# Duplicate delivery is harmless only because jobs run one at a time. Raising it
# first needs run exclusion on the enrichment and hash-backfill tasks, a
# cross-process MusicBrainz rate limit and the guarded writes of AUD-R018.
api:    uv run python -m backend.run_server
worker: uv run python -m huey.bin.huey_consumer backend.tasks.huey_app.huey -w 1
cues:   uv run python -m huey.bin.huey_consumer backend.tasks.cue_huey_app.cue_huey -w 1
web:    cd frontend && npm run dev
