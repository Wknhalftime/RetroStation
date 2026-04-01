-- Settings key-value store, structured log table, background task tracking.

CREATE TABLE user_settings (
    key        TEXT        PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE system_logs (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    trace_id   TEXT,
    category   TEXT        NOT NULL,
    level      TEXT        NOT NULL,
    message    TEXT        NOT NULL,
    details    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_system_logs_created ON system_logs(created_at DESC);
CREATE INDEX idx_system_logs_level   ON system_logs(level);

CREATE TABLE progress_tracking (
    task_id       TEXT        PRIMARY KEY,
    task_type     TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'running',
    progress_data JSONB       NOT NULL DEFAULT '{}',
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);

CREATE INDEX idx_progress_status_time ON progress_tracking(status, updated_at);
CREATE INDEX idx_progress_type_status ON progress_tracking(task_type, status);
CREATE INDEX idx_progress_stale
    ON progress_tracking(updated_at)
    WHERE status = 'running';
