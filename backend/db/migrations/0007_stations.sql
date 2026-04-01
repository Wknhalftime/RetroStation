-- Radio stations + broadcast day calendar + deferred FKs from 0001.

CREATE TABLE stations (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    call_letters TEXT        NOT NULL UNIQUE,
    name         TEXT,
    city         TEXT,
    format_name  TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE broadcast_days (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id     UUID NOT NULL REFERENCES stations(id),
    broadcast_date DATE NOT NULL,
    UNIQUE (station_id, broadcast_date)
);

CREATE INDEX idx_broadcast_days_station ON broadcast_days(station_id);

-- Deferred FKs (stations and broadcast_days now exist)
ALTER TABLE playlists  ADD COLUMN station_id      UUID REFERENCES stations(id) ON DELETE SET NULL;
ALTER TABLE log_events ADD COLUMN broadcast_day_id UUID REFERENCES broadcast_days(id);

CREATE INDEX idx_playlists_station ON playlists(station_id);
