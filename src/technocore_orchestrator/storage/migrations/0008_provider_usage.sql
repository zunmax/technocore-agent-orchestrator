BEGIN IMMEDIATE;

ALTER TABLE invocations ADD COLUMN input_tokens INTEGER
    CHECK (input_tokens IS NULL OR input_tokens >= 0);
ALTER TABLE invocations ADD COLUMN output_tokens INTEGER
    CHECK (output_tokens IS NULL OR output_tokens >= 0);
ALTER TABLE invocations ADD COLUMN cache_read_input_tokens INTEGER
    CHECK (cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0);
ALTER TABLE invocations ADD COLUMN cache_creation_input_tokens INTEGER
    CHECK (cache_creation_input_tokens IS NULL OR cache_creation_input_tokens >= 0);
ALTER TABLE invocations ADD COLUMN provider_turns INTEGER
    CHECK (provider_turns IS NULL OR provider_turns >= 0);

PRAGMA user_version = 8;
COMMIT;
