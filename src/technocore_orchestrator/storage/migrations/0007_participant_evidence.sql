BEGIN IMMEDIATE;

CREATE TABLE run_participants (
    participant_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (
        role IN ('supervisor', 'planner', 'implementer', 'reviewer', 'verifier')
    ),
    did TEXT NOT NULL,
    harness TEXT CHECK (
        harness IS NULL OR harness IN ('codex', 'claude', 'fake')
    ),
    model TEXT,
    cli_name TEXT,
    cli_version TEXT,
    executable_path TEXT,
    executable_sha256 TEXT CHECK (
        executable_sha256 IS NULL OR length(executable_sha256) = 64
    ),
    executable_size_bytes INTEGER CHECK (
        executable_size_bytes IS NULL OR executable_size_bytes >= 1
    ),
    structured_output INTEGER CHECK (structured_output IS NULL OR structured_output IN (0, 1)),
    resumable INTEGER CHECK (resumable IS NULL OR resumable IN (0, 1)),
    recorded_at TEXT NOT NULL,
    UNIQUE (run_id, role)
) STRICT;

CREATE INDEX run_participants_run_id_idx ON run_participants(run_id, participant_id);

PRAGMA user_version = 7;
COMMIT;
