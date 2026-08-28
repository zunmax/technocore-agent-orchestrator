PRAGMA foreign_keys = OFF;
BEGIN IMMEDIATE;

CREATE TABLE runs_new (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    config_digest TEXT NOT NULL CHECK (length(config_digest) = 64),
    repository_path TEXT NOT NULL,
    base_commit TEXT NOT NULL CHECK (length(base_commit) = 40),
    state TEXT NOT NULL CHECK (
        state IN (
            'created', 'planning', 'challenging', 'finalizing', 'ready',
            'implementing', 'reviewing', 'verifying', 'completed', 'failed',
            'canceled'
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

INSERT INTO runs_new (
    run_id, task_id, config_digest, repository_path, base_commit,
    state, created_at, updated_at
)
SELECT
    run_id, task_id, config_digest, repository_path, base_commit,
    state, created_at, updated_at
FROM runs;

DROP TABLE runs;
ALTER TABLE runs_new RENAME TO runs;

PRAGMA user_version = 10;
COMMIT;
PRAGMA foreign_keys = ON;
