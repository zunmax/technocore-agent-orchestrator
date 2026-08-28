BEGIN IMMEDIATE;

CREATE TABLE collaboration_messages_new (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    task_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN (
        'plan_proposed',
        'plan_challenged',
        'plan_finalized',
        'handoff_acknowledged',
        'implementation_submitted',
        'candidate_ready',
        'findings_resolved',
        'review_submitted'
    )),
    sender TEXT NOT NULL CHECK (sender IN ('planner', 'implementer', 'reviewer')),
    reply_to TEXT,
    text TEXT NOT NULL CHECK (length(text) BETWEEN 1 AND 600),
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    envelope_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    transport_status TEXT NOT NULL CHECK (
        transport_status IN ('local_pending', 'published', 'uncertain', 'failed')
    ),
    technocore_seq INTEGER CHECK (technocore_seq IS NULL OR technocore_seq >= 1),
    UNIQUE (run_id, technocore_seq)
) STRICT;

INSERT INTO collaboration_messages_new (
    event_id, run_id, task_id, kind, sender, reply_to, text, payload_json,
    payload_sha256, envelope_json, created_at, transport_status, technocore_seq
)
SELECT
    event_id, run_id, task_id, kind, sender, reply_to, text, payload_json,
    payload_sha256, envelope_json, created_at, transport_status, technocore_seq
FROM collaboration_messages;

DROP TABLE collaboration_messages;
ALTER TABLE collaboration_messages_new RENAME TO collaboration_messages;

CREATE INDEX collaboration_messages_run_order
ON collaboration_messages (run_id, created_at, event_id);

PRAGMA user_version = 11;
COMMIT;
