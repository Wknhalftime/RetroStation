-- Watcher layer: folder hash tree, staged hashes, file status tracking.

CREATE TABLE library_folders (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id   UUID        REFERENCES library_folders(id) ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    full_path   TEXT        NOT NULL UNIQUE,
    folder_hash TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_library_folders_parent ON library_folders(parent_id);

CREATE TABLE library_folder_staged_hashes (
    folder_id       UUID        NOT NULL REFERENCES library_folders(id) ON DELETE CASCADE,
    new_hash        TEXT        NOT NULL,
    staged_by_task  TEXT        NOT NULL,
    staged_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (folder_id, staged_by_task)
);

ALTER TABLE library_files
    ADD COLUMN file_status TEXT NOT NULL DEFAULT 'present';

CREATE INDEX idx_library_files_file_status
    ON library_files(file_status)
    WHERE file_status != 'present';
