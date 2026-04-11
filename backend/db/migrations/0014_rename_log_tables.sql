-- 0014_rename_log_tables.sql
-- Rename broadcast-layer tables from Log* to descriptive names.
-- PostgreSQL auto-updates FK constraints but NOT named indexes.

-- Table renames
ALTER TABLE log_artists RENAME TO broadcast_artists;
ALTER TABLE log_identities RENAME TO track_identities;
ALTER TABLE log_events RENAME TO play_events;

-- Column rename (track_identities.artist_id → broadcast_artist_id)
ALTER TABLE track_identities RENAME COLUMN artist_id TO broadcast_artist_id;

-- Index renames (7 explicit — PostgreSQL does not auto-rename these)
ALTER INDEX idx_log_artists_embedding RENAME TO idx_broadcast_artists_embedding;
ALTER INDEX idx_log_identities_embedding RENAME TO idx_track_identities_embedding;
ALTER INDEX idx_log_identities_artist RENAME TO idx_track_identities_broadcast_artist;
ALTER INDEX idx_log_identities_status RENAME TO idx_track_identities_status;
ALTER INDEX idx_log_events_playlist RENAME TO idx_play_events_playlist;
ALTER INDEX idx_log_events_identity RENAME TO idx_play_events_identity;
ALTER INDEX idx_log_events_played_at RENAME TO idx_play_events_played_at;
