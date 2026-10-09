-- AUD-R026 (D15): the local-artist linker's stamp. mb_lookup_at is when it last looked the
-- artist up, mb_lookup_outcome what it decided. Both are NULL until the first lookup. An
-- unlinked local artist is due again only when a present file of it is indexed after
-- mb_lookup_at. The deferred name search (D15b) extends the outcome list in its own migration.
ALTER TABLE artists
    ADD COLUMN mb_lookup_at TIMESTAMPTZ,
    ADD COLUMN mb_lookup_outcome TEXT,
    ADD CONSTRAINT artists_mb_lookup_pair
        CHECK ((mb_lookup_at IS NULL) = (mb_lookup_outcome IS NULL)),
    ADD CONSTRAINT artists_mb_lookup_outcome_known
        CHECK (mb_lookup_outcome IN ('linked', 'ambiguous', 'tag_mismatch', 'special_purpose',
                                     'duplicate', 'no_evidence'));
