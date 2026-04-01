-- pgvector extension + embedding columns on 4 tables + HNSW indexes.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE log_artists    ADD COLUMN embedding vector(1024);
ALTER TABLE log_identities ADD COLUMN embedding vector(1024);
ALTER TABLE works          ADD COLUMN embedding vector(1024);
ALTER TABLE recordings     ADD COLUMN embedding vector(1024);

CREATE INDEX idx_log_artists_embedding
    ON log_artists USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_log_identities_embedding
    ON log_identities USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_works_embedding
    ON works USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

CREATE INDEX idx_recordings_embedding
    ON recordings USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);
