-- Work merges phase 3b PR 5c (SPEC D40, D55-D58; AUD-R089): Lance's lasting "Keep apart" mark.
--
-- One row per pair of works the merge planner must never join to each other: the two work ids
-- in plain string order (the lower first, under COLLATE "C" so the order is the app's whatever
-- the database's collation), when, and a note the writer sets. Either work's delete takes its
-- marks. A merge re-points its sources' marks to the merged work, and the merge log captures
-- them (S10), so its restore puts them back.

SET LOCAL lock_timeout = '5s';

CREATE TABLE work_keep_apart (
    first_work_id  TEXT        NOT NULL REFERENCES works (id) ON DELETE CASCADE,
    second_work_id TEXT        NOT NULL REFERENCES works (id) ON DELETE CASCADE,
    kept_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    note           TEXT        NOT NULL DEFAULT '',
    PRIMARY KEY (first_work_id, second_work_id),
    CONSTRAINT work_keep_apart_in_order
        CHECK (first_work_id COLLATE "C" < second_work_id COLLATE "C")
);
CREATE INDEX idx_work_keep_apart_second ON work_keep_apart (second_work_id);

CREATE TRIGGER work_merge_capture AFTER INSERT OR UPDATE OR DELETE ON work_keep_apart
    FOR EACH ROW WHEN (current_setting('retrostation.merge_id', true) <> '')
    EXECUTE FUNCTION work_merge_capture('first_work_id', 'second_work_id');
