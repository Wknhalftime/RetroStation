-- Drop the HNSW indexes on the song and artist embeddings. No query reads a vector (AUD-R020):
-- both indexes had 0 scans on dev (207 MB and 52 MB). match_status is indexed, so a status write
-- is never a HOT update, and each one also inserted the row's vector into its HNSW index. On the
-- 2026-10-18 matching profile that was 2.29 ms per song status write, against 0.026 ms without
-- the index; status writes took 132 s of a 297 s Re-run Matching. The vectors stay where they
-- are; rollback_0042_drop_embedding_hnsw.sql builds the indexes again if a reader ever needs them.
-- The works / recordings indexes are left: they are empty (16 kB) and see no status writes.
DROP INDEX IF EXISTS idx_track_identities_embedding;
DROP INDEX IF EXISTS idx_broadcast_artists_embedding;
