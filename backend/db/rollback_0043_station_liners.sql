-- Rollback for 0043_station_liners.sql. Run by hand, with the app stopped, in one
-- transaction:
--   psql -1 -f backend/db/rollback_0043_station_liners.sql <database>
-- This deletes every station's liner rows and cadence; the liner files stay on disk under
-- the liners folder and can be removed by hand.
DROP TABLE IF EXISTS station_liners;
ALTER TABLE stations DROP COLUMN IF EXISTS songs_between_liners;
DELETE FROM schema_migrations WHERE version = '0043_station_liners';
