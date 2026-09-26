-- Missing-file reconciliation and the matcher look files up by recording_mbid
-- alone; the only index holding it leads with enrichment_status.
CREATE INDEX idx_library_files_recording_mbid ON library_files (recording_mbid);
