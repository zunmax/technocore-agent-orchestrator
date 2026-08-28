BEGIN IMMEDIATE;

CREATE TABLE worktrees (
    worktree_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('planner', 'implementer', 'reviewer', 'verifier')),
    path TEXT NOT NULL UNIQUE,
    branch TEXT,
    writable INTEGER NOT NULL CHECK (writable IN (0, 1)),
    initial_commit TEXT NOT NULL CHECK (length(initial_commit) = 40),
    status TEXT NOT NULL CHECK (status IN ('active', 'retained', 'removed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (run_id, role)
) STRICT;

CREATE TABLE worktree_observations (
    observation_id INTEGER PRIMARY KEY,
    worktree_id INTEGER NOT NULL REFERENCES worktrees(worktree_id) ON DELETE RESTRICT,
    head_commit TEXT NOT NULL CHECK (length(head_commit) = 40),
    clean INTEGER NOT NULL CHECK (clean IN (0, 1)),
    observed_at TEXT NOT NULL
) STRICT;

CREATE INDEX worktree_observations_worktree_idx
    ON worktree_observations(worktree_id, observation_id);

PRAGMA user_version = 3;
COMMIT;
