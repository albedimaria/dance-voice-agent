-- 006_call_latency.sql

-- Per-call latency aggregates, written by the voice agent at call teardown.
-- Values are averages across the call's response turns (one turn = one user
-- utterance answered). Nullable: rows created before this migration, and calls
-- with no measurable turn (e.g. caller hung up immediately), stay NULL.
ALTER TABLE call_logs
    ADD COLUMN IF NOT EXISTS avg_response_ms int,  -- STT-final -> first audio frame out
    ADD COLUMN IF NOT EXISTS avg_ttft_ms     int,  -- STT-final -> first LLM token
    ADD COLUMN IF NOT EXISTS n_turns         int;  -- number of measured response turns
