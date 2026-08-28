BEGIN IMMEDIATE;

CREATE TABLE invocations (
    invocation_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('planner', 'implementer', 'reviewer')),
    attempt INTEGER NOT NULL CHECK (attempt >= 1 AND attempt <= 100),
    harness TEXT NOT NULL CHECK (harness IN ('codex', 'claude', 'fake')),
    status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed', 'canceled')),
    timeout_seconds REAL NOT NULL CHECK (timeout_seconds > 0 AND timeout_seconds <= 7200),
    output_limit_bytes INTEGER NOT NULL CHECK (
        output_limit_bytes >= 1 AND output_limit_bytes <= 10485760
    ),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    termination_reason TEXT CHECK (
        termination_reason IS NULL OR
        termination_reason IN ('exited', 'timed_out', 'output_limit_exceeded')
    ),
    returncode INTEGER,
    duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    stdout_sha256 TEXT CHECK (stdout_sha256 IS NULL OR length(stdout_sha256) = 64),
    stderr_sha256 TEXT CHECK (stderr_sha256 IS NULL OR length(stderr_sha256) = 64),
    result_sha256 TEXT CHECK (result_sha256 IS NULL OR length(result_sha256) = 64),
    error_category TEXT CHECK (
        error_category IS NULL OR
        error_category IN (
            'configuration', 'preflight', 'protocol', 'state', 'storage',
            'identity', 'transport', 'execution', 'internal'
        )
    ),
    CHECK (
        (status = 'started' AND ended_at IS NULL AND termination_reason IS NULL AND
            returncode IS NULL AND duration_seconds IS NULL AND stdout_sha256 IS NULL AND
            stderr_sha256 IS NULL AND result_sha256 IS NULL AND error_category IS NULL) OR
        (status = 'succeeded' AND ended_at IS NOT NULL AND duration_seconds IS NOT NULL AND
            termination_reason IS NOT NULL AND returncode IS NOT NULL AND
            stdout_sha256 IS NOT NULL AND stderr_sha256 IS NOT NULL AND
            result_sha256 IS NOT NULL AND error_category IS NULL) OR
        (status = 'failed' AND ended_at IS NOT NULL AND duration_seconds IS NOT NULL AND
            result_sha256 IS NULL AND error_category IS NOT NULL) OR
        (status = 'canceled' AND ended_at IS NOT NULL AND duration_seconds IS NOT NULL AND
            result_sha256 IS NULL AND error_category IS NULL)
    ),
    UNIQUE (run_id, role, attempt)
) STRICT;

CREATE INDEX invocations_run_id_idx ON invocations(run_id, invocation_id);

CREATE TABLE checks (
    check_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    candidate_commit TEXT NOT NULL CHECK (length(candidate_commit) = 40),
    command_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
    termination_reason TEXT NOT NULL CHECK (
        termination_reason IN ('exited', 'timed_out', 'output_limit_exceeded')
    ),
    returncode INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL CHECK (duration_seconds >= 0),
    stdout_sha256 TEXT NOT NULL CHECK (length(stdout_sha256) = 64),
    stderr_sha256 TEXT NOT NULL CHECK (length(stderr_sha256) = 64),
    UNIQUE (run_id, candidate_commit, command_id),
    UNIQUE (run_id, candidate_commit, ordinal)
) STRICT;

CREATE INDEX checks_run_id_idx ON checks(run_id, check_id);

PRAGMA user_version = 5;
COMMIT;
