-- Add `reason_code` (stable enum key) and `reason_detail` (formatted UI string)
-- to both broadcast_artists and track_identities. Both columns are nullable;
-- NULL = "no reason (auto-matched or pending)". NO BEGIN/COMMIT — the migration
-- runner (backend/db/migrations.py) wraps every .sql file in conn.transaction().

ALTER TABLE broadcast_artists ADD COLUMN IF NOT EXISTS reason_code   TEXT;
ALTER TABLE broadcast_artists ADD COLUMN IF NOT EXISTS reason_detail TEXT;

ALTER TABLE track_identities  ADD COLUMN IF NOT EXISTS reason_code   TEXT;
ALTER TABLE track_identities  ADD COLUMN IF NOT EXISTS reason_detail TEXT;
