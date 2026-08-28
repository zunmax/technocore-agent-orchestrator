BEGIN IMMEDIATE;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    config_digest TEXT NOT NULL CHECK (length(config_digest) = 64),
    repository_path TEXT NOT NULL,
    base_commit TEXT NOT NULL CHECK (length(base_commit) = 40),
    state TEXT NOT NULL CHECK (
        state IN (
            'created', 'planning', 'ready', 'implementing', 'reviewing',
            'verifying', 'completed', 'failed', 'canceled'
        )
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    sender TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    envelope_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    accepted_at TEXT NOT NULL,
    transport_status TEXT NOT NULL DEFAULT 'local_pending' CHECK (
        transport_status IN ('local_pending', 'published', 'uncertain', 'failed')
    ),
    technocore_seq INTEGER UNIQUE CHECK (technocore_seq IS NULL OR technocore_seq >= 1)
) STRICT;

CREATE INDEX events_run_accepted_idx ON events(run_id, accepted_at, event_id);

CREATE TABLE transitions (
    transition_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id) ON DELETE RESTRICT,
    previous_state TEXT NOT NULL,
    current_state TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

CREATE INDEX transitions_run_id_idx ON transitions(run_id, transition_id);

PRAGMA user_version = 1;
COMMIT;
