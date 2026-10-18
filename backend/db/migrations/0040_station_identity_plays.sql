-- How many times each station played each identity: COUNT(*) of play_events joined to their
-- playlist's station, per (station_id, identity_id). The station missing-matches report reads
-- it instead of counting play_events, which seq-scans every play. Triggers keep it equal to
-- that count wherever plays or a playlist's station change; a pair whose count reaches zero is
-- deleted. Plays of a playlist without a station are not counted, as no station reports them.

-- The migration runs at API startup, possibly beside a working ingest. Taking every lock it
-- needs first, in the order ingest writes these tables, makes it wait for ingest instead of
-- deadlocking with it halfway through; it also keeps plays out until the triggers and the
-- backfill are in place.
LOCK TABLE stations, playlists, track_identities, play_events IN SHARE ROW EXCLUSIVE MODE;

CREATE TABLE station_identity_plays (
    station_id  UUID NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    identity_id UUID NOT NULL REFERENCES track_identities(id) ON DELETE CASCADE,
    play_count  BIGINT NOT NULL,
    PRIMARY KEY (station_id, identity_id)
);

CREATE INDEX idx_station_identity_plays_identity ON station_identity_plays (identity_id);

-- Adds each (station_id, identity_id, delta) row to the counts, then drops the pairs it took
-- to zero. Callers pass one row per pair. Additions upsert, so two transactions adding the
-- same new pair both land.
CREATE FUNCTION station_identity_plays_apply(
    deltas station_identity_plays[]
) RETURNS VOID
LANGUAGE sql AS $$
    INSERT INTO station_identity_plays AS sip (station_id, identity_id, play_count)
    SELECT d.station_id, d.identity_id, d.play_count
    FROM unnest(deltas) AS d
    WHERE d.play_count > 0
    ON CONFLICT (station_id, identity_id)
    DO UPDATE SET play_count = sip.play_count + EXCLUDED.play_count;

    UPDATE station_identity_plays AS sip
    SET play_count = sip.play_count + d.play_count
    FROM unnest(deltas) AS d
    WHERE d.play_count < 0
      AND sip.station_id = d.station_id
      AND sip.identity_id = d.identity_id;

    DELETE FROM station_identity_plays AS sip
    USING unnest(deltas) AS d
    WHERE d.play_count < 0
      AND sip.station_id = d.station_id
      AND sip.identity_id = d.identity_id
      AND sip.play_count <= 0;
$$;

-- play_events INSERT / DELETE / UPDATE, one call per statement; each trigger passes only the
-- transition tables its event has. A play whose playlist is already gone (the cascade of a
-- playlist delete) joins no station: playlists_identity_plays_delete took it off first.
CREATE FUNCTION play_events_identity_plays_sync() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    deltas station_identity_plays[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Ingest inserts one play per statement, so this branch runs once per play: the
        -- upsert is inlined, as building deltas for the helper costs several times as much.
        INSERT INTO station_identity_plays AS sip (station_id, identity_id, play_count)
        SELECT pl.station_id, r.identity_id, COUNT(*)
        FROM new_rows r JOIN playlists pl ON pl.id = r.playlist_id
        WHERE pl.station_id IS NOT NULL
        GROUP BY pl.station_id, r.identity_id
        ON CONFLICT (station_id, identity_id)
        DO UPDATE SET play_count = sip.play_count + EXCLUDED.play_count;
        RETURN NULL;
    ELSIF TG_OP = 'DELETE' THEN
        SELECT array_agg(ROW(c.station_id, c.identity_id, -c.n)::station_identity_plays)
        INTO deltas
        FROM (
            SELECT pl.station_id, r.identity_id, COUNT(*) AS n
            FROM old_rows r JOIN playlists pl ON pl.id = r.playlist_id
            WHERE pl.station_id IS NOT NULL
            GROUP BY pl.station_id, r.identity_id
        ) AS c;
    ELSE
        SELECT array_agg(ROW(c.station_id, c.identity_id, c.n)::station_identity_plays)
        INTO deltas
        FROM (
            SELECT pl.station_id, r.identity_id, SUM(r.sign) AS n
            FROM (
                SELECT playlist_id, identity_id, 1 AS sign FROM new_rows
                UNION ALL
                SELECT playlist_id, identity_id, -1 AS sign FROM old_rows
            ) AS r
            JOIN playlists pl ON pl.id = r.playlist_id
            WHERE pl.station_id IS NOT NULL
            GROUP BY pl.station_id, r.identity_id
            HAVING SUM(r.sign) <> 0
        ) AS c;
    END IF;
    IF deltas IS NOT NULL THEN
        PERFORM station_identity_plays_apply(deltas);
    END IF;
    RETURN NULL;
END;
$$;

-- A playlist's plays leave its station's counts before the playlist goes: the plays' own
-- cascaded delete runs after the playlist row is gone and can no longer see its station.
-- When the station itself is being deleted, its counts go by FK cascade instead.
CREATE FUNCTION playlists_identity_plays_delete() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    deltas station_identity_plays[];
BEGIN
    IF EXISTS (SELECT 1 FROM stations WHERE id = OLD.station_id) THEN
        SELECT array_agg(ROW(OLD.station_id, c.identity_id, -c.n)::station_identity_plays)
        INTO deltas
        FROM (
            SELECT identity_id, COUNT(*) AS n
            FROM play_events
            WHERE playlist_id = OLD.id
            GROUP BY identity_id
        ) AS c;
        IF deltas IS NOT NULL THEN
            PERFORM station_identity_plays_apply(deltas);
        END IF;
    END IF;
    RETURN OLD;
END;
$$;

-- A playlist moved to another station takes its plays from the old station's counts to the
-- new one's.
CREATE FUNCTION playlists_identity_plays_move() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    deltas station_identity_plays[];
BEGIN
    SELECT array_agg(ROW(c.station_id, c.identity_id, c.n)::station_identity_plays)
    INTO deltas
    FROM (
        SELECT OLD.station_id AS station_id, identity_id, -COUNT(*) AS n
        FROM play_events
        WHERE playlist_id = OLD.id AND OLD.station_id IS NOT NULL
        GROUP BY identity_id
        UNION ALL
        SELECT NEW.station_id, identity_id, COUNT(*)
        FROM play_events
        WHERE playlist_id = NEW.id AND NEW.station_id IS NOT NULL
        GROUP BY identity_id
    ) AS c;
    IF deltas IS NOT NULL THEN
        PERFORM station_identity_plays_apply(deltas);
    END IF;
    RETURN NULL;
END;
$$;

-- DELETE, not TRUNCATE: a TRUNCATE ... CASCADE that reaches station_identity_plays through its
-- stations FK has it open, and a nested TRUNCATE of it would fail.
CREATE FUNCTION play_events_identity_plays_truncate() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM station_identity_plays;
    RETURN NULL;
END;
$$;

CREATE TRIGGER play_events_identity_plays_insert
    AFTER INSERT ON play_events
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION play_events_identity_plays_sync();

CREATE TRIGGER play_events_identity_plays_delete
    AFTER DELETE ON play_events
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT EXECUTE FUNCTION play_events_identity_plays_sync();

CREATE TRIGGER play_events_identity_plays_update
    AFTER UPDATE ON play_events
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT EXECUTE FUNCTION play_events_identity_plays_sync();

CREATE TRIGGER play_events_identity_plays_truncate
    AFTER TRUNCATE ON play_events
    FOR EACH STATEMENT EXECUTE FUNCTION play_events_identity_plays_truncate();

CREATE TRIGGER playlists_identity_plays_delete
    BEFORE DELETE ON playlists
    FOR EACH ROW EXECUTE FUNCTION playlists_identity_plays_delete();

CREATE TRIGGER playlists_identity_plays_move
    AFTER UPDATE OF station_id ON playlists
    FOR EACH ROW
    WHEN (OLD.station_id IS DISTINCT FROM NEW.station_id)
    EXECUTE FUNCTION playlists_identity_plays_move();

INSERT INTO station_identity_plays (station_id, identity_id, play_count)
SELECT pl.station_id, pe.identity_id, COUNT(*)
FROM play_events pe
JOIN playlists pl ON pl.id = pe.playlist_id
WHERE pl.station_id IS NOT NULL
GROUP BY pl.station_id, pe.identity_id;
