-- S10's restore (work merges, phase 3b PR 3, Q18; AUD-R079): undo every logged merge, newest
-- first, from its rollback map. Run by hand, with the app stopped, in one transaction:
--   psql -1 -f backend/db/rollback_work_merges.sql <database>
-- For each merge still 'merged': the rows it updated or deleted come back as they were, the
-- rows it inserted go (with them its pins, stored lines and credits), its redirects go, and it
-- is marked 'rolled_back'. Parents come back before children and go after them; preferred
-- copies are cleared first and put back last, since they point into files being moved back.
SET LOCAL lock_timeout = '5s';
SELECT set_config('retrostation.merge_id', '', true);

DO $restore$
DECLARE
    m RECORD;
    t RECORD;
    w RECORD;
    cols TEXT;
    vals TEXT;
    keyed TEXT;
    image JSONB;
BEGIN
    DROP TABLE IF EXISTS pg_temp.merge_restore_keys;
    CREATE TEMP TABLE merge_restore_keys (table_name TEXT, keys TEXT[], parent BOOLEAN, ord INT)
        ON COMMIT DROP;
    INSERT INTO merge_restore_keys VALUES
        ('artists', ARRAY['id'], true, 1),
        ('artist_names', ARRAY['id'], true, 2),
        ('works', ARRAY['id'], true, 3),
        ('recordings', ARRAY['id'], true, 4),
        ('performances', ARRAY['id'], true, 5),
        ('library_files', ARRAY['id'], false, 6),
        ('performance_credits', ARRAY['performance_id', 'position'], false, 7),
        ('song_masters', ARRAY['id'], false, 8),
        ('format_overrides', ARRAY['id'], false, 9),
        ('version_entry_states', ARRAY['work_id', 'version_text'], false, 10),
        ('matches', ARRAY['id'], false, 11),
        ('credit_pick_proposals', ARRAY['id'], false, 12);

    FOR m IN SELECT id FROM work_merges WHERE status = 'merged' ORDER BY seq DESC LOOP
        -- Preferred copies point into files about to move back: clear them first.
        UPDATE performances p SET preferred_copy_id = NULL
          FROM work_merge_rows r
         WHERE r.merge_id = m.id AND r.table_name = 'performances'
           AND p.id::text = r.row_key ->> 0;

        FOR t IN SELECT * FROM merge_restore_keys ORDER BY ord LOOP
            SELECT string_agg(quote_ident(c.column_name), ', ' ORDER BY c.ordinal_position),
                   string_agg('src.' || quote_ident(c.column_name), ', '
                              ORDER BY c.ordinal_position)
              INTO cols, vals
              FROM information_schema.columns c
             WHERE c.table_schema = 'public' AND c.table_name = t.table_name;
            SELECT string_agg(format('x.%I::text = $2 ->> %s', k, n - 1), ' AND ')
              INTO keyed
              FROM unnest(t.keys) WITH ORDINALITY AS u (k, n);

            -- A child's rows the merge inserted go before its old rows come back (a unique key
            -- such as one master per work may be held by the new row).
            IF NOT t.parent THEN
                FOR w IN SELECT row_key FROM work_merge_rows
                          WHERE merge_id = m.id AND table_name = t.table_name
                            AND before IS NULL LOOP
                    EXECUTE format('DELETE FROM %I x WHERE %s', t.table_name,
                                   replace(keyed, '$2', '$1'))
                    USING w.row_key;
                END LOOP;
            END IF;

            FOR w IN SELECT row_key, before FROM work_merge_rows
                      WHERE merge_id = m.id AND table_name = t.table_name
                        AND before IS NOT NULL LOOP
                image := w.before;
                IF t.table_name = 'performances' THEN
                    image := jsonb_set(image, '{preferred_copy_id}', 'null');
                END IF;
                EXECUTE format(
                    'INSERT INTO %I SELECT (jsonb_populate_record(NULL::%I, $1)).* '
                    'ON CONFLICT DO NOTHING', t.table_name, t.table_name)
                USING image;
                EXECUTE format(
                    'UPDATE %I x SET (%s) = (SELECT %s FROM jsonb_populate_record(NULL::%I, $1) '
                    'src) WHERE %s', t.table_name, cols, vals, t.table_name, keyed)
                USING image, w.row_key;
            END LOOP;
        END LOOP;

        -- Parents the merge inserted go last, children first (performances before works).
        FOR t IN SELECT * FROM merge_restore_keys WHERE parent ORDER BY ord DESC LOOP
            SELECT string_agg(format('x.%I::text = $1 ->> %s', k, n - 1), ' AND ')
              INTO keyed
              FROM unnest(t.keys) WITH ORDINALITY AS u (k, n);
            FOR w IN SELECT row_key FROM work_merge_rows
                      WHERE merge_id = m.id AND table_name = t.table_name
                        AND before IS NULL LOOP
                EXECUTE format('DELETE FROM %I x WHERE %s', t.table_name, keyed)
                USING w.row_key;
            END LOOP;
        END LOOP;

        -- Preferred copies, as they were.
        UPDATE performances p SET preferred_copy_id = (r.before ->> 'preferred_copy_id')::uuid
          FROM work_merge_rows r
         WHERE r.merge_id = m.id AND r.table_name = 'performances' AND r.before IS NOT NULL
           AND p.id::text = r.row_key ->> 0;

        DELETE FROM work_redirects WHERE merge_id = m.id;
        UPDATE work_merges SET status = 'rolled_back' WHERE id = m.id;
    END LOOP;
END
$restore$;
