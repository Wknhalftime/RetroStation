-- Rebuild the HNSW indexes 0042 dropped, with 0005's parameters, then forget the migration.
-- Building them over every stored vector takes a while (about 30k songs and 7k artists on dev).
CREATE INDEX IF NOT EXISTS idx_track_identities_embedding
    ON track_identities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
CREATE INDEX IF NOT EXISTS idx_broadcast_artists_embedding
    ON broadcast_artists USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
DELETE FROM schema_migrations WHERE version = '0042_drop_embedding_hnsw';
