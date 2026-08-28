BEGIN IMMEDIATE;

ALTER TABLE worktree_observations
ADD COLUMN changed_paths_json TEXT NOT NULL DEFAULT '[]'
CHECK (json_valid(changed_paths_json) AND json_type(changed_paths_json) = 'array');

PRAGMA user_version = 12;
COMMIT;
