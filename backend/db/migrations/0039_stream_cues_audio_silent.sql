-- 0039_stream_cues_audio_silent.sql
-- D111 (issue #146): a silent file is skipped, not played as dead air. The cue analysis marks
-- audio it measured at the meter's floor (-70 LUFS) as silent, keyed by audio_hash like the rest
-- of the row (S3). A silent row is also a fallback row (analysis_failed = true, D52). Schedule
-- resolution gives a song whose audio is silent no file, so tune-in and the walk skip it.
ALTER TABLE stream_cues ADD COLUMN audio_silent BOOLEAN NOT NULL DEFAULT false;

-- S4 and S4b, once at deployment: delete the stored failed rows, and the analysed rows already
-- measured at the floor (gain_db >= +52.0 dB, i.e. SILENT_GAIN_DB at the -18 LUFS target: silent
-- audio whose autocue output had valid full-length cues), so each file is analysed again under
-- D111 and a silent one gets its verdict. 22 rows on 2026-10-16: 14 stalls, 2 autocue stubs,
-- 3 "no metadata", 3 at the floor. No analyser-version bump: the analyser is unchanged, and a
-- bump would re-analyse the whole library (D20).
DELETE FROM stream_cues WHERE analysis_failed OR gain_db >= 52.0;
