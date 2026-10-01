-- Rollback for 0034_station_call_letters_ci.sql. Run by hand with the app stopped, in one
-- transaction:
--   psql -1 -f backend/db/rollback_0034_station_call_letters_ci.sql <database>
-- Drops the case-insensitive unique index; the case-sensitive stations_call_letters_key is
-- untouched, so twins differing only by case can be stored again (D72 is lifted).
DROP INDEX IF EXISTS idx_stations_call_letters_lower;
DELETE FROM schema_migrations WHERE version = '0034_station_call_letters_ci';
