-- MusicBrainz cache fill, phase 4.1 PR 1 (SPEC S1; design C42; AUD-R042, AUD-R090).
-- 0020 made mb_cache UNLOGGED, so a crash empties it (the dev cache was emptied on 2026-11-06 and
-- again on 2026-11-11). From 4.2 on, matching reads only the cache (C29), so it becomes LOGGED
-- again. SET LOGGED keeps every row: it rewrites the table into the WAL under an ACCESS EXCLUSIVE
-- lock, so a reader holding the table fails the deploy at the lock timeout instead of making it
-- wait (C45). It copies no rows, so it needs no ANALYZE. No rollback script: from 2a on, the
-- rollback is the dump (2a's D4).
SET LOCAL lock_timeout = '5s';

ALTER TABLE mb_cache SET LOGGED;
