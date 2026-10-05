-- Reverse of 0019_add_reason_columns.sql. NOT applied automatically by the
-- migration runner — lives outside migrations/ to avoid being picked up by
-- its glob (MIGRATIONS_DIR.glob("*.sql") in backend/db/migrations.py). Run manually with
-- psql when needed.

ALTER TABLE broadcast_artists DROP COLUMN IF EXISTS reason_code;
ALTER TABLE broadcast_artists DROP COLUMN IF EXISTS reason_detail;

ALTER TABLE track_identities  DROP COLUMN IF EXISTS reason_code;
ALTER TABLE track_identities  DROP COLUMN IF EXISTS reason_detail;
