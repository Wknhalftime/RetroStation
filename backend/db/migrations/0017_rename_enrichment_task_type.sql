-- 0017_rename_enrichment_task_type.sql
-- Rename legacy 'enrichment' progress rows to 'library_enrichment' so they parse
-- as TaskType.LIBRARY_ENRICHMENT after the enum split.
UPDATE progress_tracking SET task_type = 'library_enrichment' WHERE task_type = 'enrichment';
