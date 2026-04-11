-- 0016_rename_global_mapping_rules.sql
-- Rename global_mapping_rules table to mapping_rules.
ALTER TABLE global_mapping_rules RENAME TO mapping_rules;
ALTER INDEX idx_rules_priority RENAME TO idx_mapping_rules_priority;
