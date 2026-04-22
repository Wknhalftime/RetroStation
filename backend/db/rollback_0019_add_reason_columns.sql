-- Reverse of 0019_add_reason_columns.sql. NOT applied automatically by the
-- migration runner — lives outside migrations/ to avoid being picked up by
-- its glob (see .claude/CLAUDE.md > Database Migrations). Run manually with
-- psql when needed.

ALTER TABLE broadcast_artists DROP COLUMN IF EXISTS reason_code;
ALTER TABLE broadcast_artists DROP COLUMN IF EXISTS reason_detail;

ALTER TABLE track_identities  DROP COLUMN IF EXISTS reason_code;
ALTER TABLE track_identities  DROP COLUMN IF EXISTS reason_detail;
