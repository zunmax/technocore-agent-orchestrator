BEGIN IMMEDIATE;

CREATE TABLE role_results (
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('planner', 'implementer', 'reviewer')),
    attempt INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 100),
    result_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, role, attempt)
) STRICT;

PRAGMA user_version = 4;
COMMIT;
