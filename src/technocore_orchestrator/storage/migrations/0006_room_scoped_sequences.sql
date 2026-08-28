BEGIN IMMEDIATE;

CREATE TABLE events_new (
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
    technocore_seq INTEGER CHECK (technocore_seq IS NULL OR technocore_seq >= 1),
    UNIQUE (run_id, technocore_seq)
) STRICT;

INSERT INTO events_new SELECT * FROM events;

CREATE TABLE transitions_new (
    transition_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    event_id TEXT NOT NULL UNIQUE REFERENCES events_new(event_id) ON DELETE RESTRICT,
    previous_state TEXT NOT NULL,
    current_state TEXT NOT NULL,
    created_at TEXT NOT NULL
) STRICT;

INSERT INTO transitions_new SELECT * FROM transitions;

DROP TABLE transitions;
DROP TABLE events;
ALTER TABLE events_new RENAME TO events;
ALTER TABLE transitions_new RENAME TO transitions;

CREATE INDEX events_run_accepted_idx ON events(run_id, accepted_at, event_id);
CREATE INDEX transitions_run_id_idx ON transitions(run_id, transition_id);

PRAGMA user_version = 6;
COMMIT;
