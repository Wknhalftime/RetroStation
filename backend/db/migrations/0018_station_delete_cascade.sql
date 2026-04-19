-- Station deletion cascade: remove station and everything it owns atomically.
--
-- stations
--   ├─► broadcast_days          CASCADE  (new)
--   │     └─► play_events       CASCADE  (new, via broadcast_day_id)
--   └─► playlists               CASCADE  (was SET NULL)
--         └─► play_events       CASCADE  (already in place since 0001)
--
-- Also renames log_events_broadcast_day_id_fkey → play_events_broadcast_day_id_fkey
-- to match the post-0014 table name (PostgreSQL preserves constraint names
-- across RENAME TABLE).

ALTER TABLE broadcast_days
    DROP CONSTRAINT broadcast_days_station_id_fkey,
    ADD  CONSTRAINT broadcast_days_station_id_fkey
         FOREIGN KEY (station_id) REFERENCES stations(id) ON DELETE CASCADE;

ALTER TABLE playlists
    DROP CONSTRAINT playlists_station_id_fkey,
    ADD  CONSTRAINT playlists_station_id_fkey
         FOREIGN KEY (station_id) REFERENCES stations(id) ON DELETE CASCADE;

ALTER TABLE play_events
    DROP CONSTRAINT log_events_broadcast_day_id_fkey,
    ADD  CONSTRAINT play_events_broadcast_day_id_fkey
         FOREIGN KEY (broadcast_day_id) REFERENCES broadcast_days(id) ON DELETE CASCADE;
