BEGIN IMMEDIATE;

CREATE TABLE identity_nonces (
    did TEXT NOT NULL,
    room_sha256 TEXT NOT NULL CHECK (length(room_sha256) = 64),
    last_nonce INTEGER NOT NULL CHECK (last_nonce >= 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (did, room_sha256)
) STRICT, WITHOUT ROWID;

PRAGMA user_version = 2;
COMMIT;
