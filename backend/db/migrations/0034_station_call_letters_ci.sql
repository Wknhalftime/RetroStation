-- 0034_station_call_letters_ci.sql
-- Call letters match in any case (D72): a station differing from another only by case is
-- blocked by a unique index on the lower-cased call letters. The case-sensitive
-- stations_call_letters_key (0007) stays; it is now redundant but harmless.
CREATE UNIQUE INDEX idx_stations_call_letters_lower ON stations (lower(call_letters));
