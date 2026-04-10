-- Rename MAN_MATCHED/MAN_REJECTED to MANUAL_MATCHED/MANUAL_REJECTED
-- in all tables that store match_status enum values.

UPDATE log_artists SET match_status = 'MANUAL_MATCHED' WHERE match_status = 'MAN_MATCHED';
UPDATE log_artists SET match_status = 'MANUAL_REJECTED' WHERE match_status = 'MAN_REJECTED';

UPDATE log_identities SET match_status = 'MANUAL_MATCHED' WHERE match_status = 'MAN_MATCHED';
UPDATE log_identities SET match_status = 'MANUAL_REJECTED' WHERE match_status = 'MAN_REJECTED';
