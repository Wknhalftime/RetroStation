-- Master file selection per work + per-station-format overrides.

CREATE TABLE song_masters (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT        NOT NULL REFERENCES works(id),
    preferred_file_id UUID        NOT NULL REFERENCES library_files(id),
    selection_method  TEXT        NOT NULL DEFAULT 'auto',
    score             INTEGER,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_id)
);

CREATE TABLE format_overrides (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    work_id           TEXT        NOT NULL REFERENCES works(id),
    format_name       TEXT        NOT NULL,
    preferred_file_id UUID        NOT NULL REFERENCES library_files(id),
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (work_id, format_name)
);

CREATE INDEX idx_format_overrides_work   ON format_overrides(work_id);
CREATE INDEX idx_format_overrides_format ON format_overrides(format_name);
