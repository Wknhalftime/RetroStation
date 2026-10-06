-- AUD-R022 (D3, D7): library files the curator rejected for this broadcast song. The matcher
-- skips each one and every file currently in the same work. No foreign key: a purged file's id is
-- inert (it can never be a candidate again).
ALTER TABLE track_identities
    ADD COLUMN rejected_file_ids UUID[] NOT NULL DEFAULT '{}';
