-- 007_eval_observability.sql
--
-- Telemetry + eval tables for the observability/eval dashboard views.
-- Written by the voice agent (service role) and the eval runner; read by the
-- dashboard as `authenticated` under RLS (same pattern as 005).
--
-- call_logs stays the business-level view (intent/outcome). These tables add
-- the per-turn telemetry, per-call cost rollup, and reproducible eval results.

-- Per-turn telemetry: one row per answered user turn.
CREATE TABLE IF NOT EXISTS turn_metrics (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id           text NOT NULL,           -- Twilio stream_sid
    turn_index        int  NOT NULL,
    ttft_ms           int,                     -- STT-final -> first LLM token (incl. tool time)
    llm_ms            int,                     -- STT-final -> last LLM token (full response)
    tts_ttfb_ms       int,                     -- first sentence -> first TTS chunk
    tts_ms            int,                     -- first sentence -> last TTS chunk
    response_ms       int,                     -- STT-final -> first audio frame out
    tool_rounds       int,
    tool_ms           int,
    prompt_tokens     int,
    completion_tokens int,
    tts_chars         int,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS turn_metrics_call_id_idx ON turn_metrics (call_id);

-- Per-call rollup + cost.
CREATE TABLE IF NOT EXISTS call_traces (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    call_id                text NOT NULL,
    phone_from             text,
    student_id             uuid,
    language               text,
    duration_seconds       int,
    n_turns                int,
    barge_in_count         int,
    total_prompt_tokens    int,
    total_completion_tokens int,
    total_tts_chars        int,
    cost_usd               numeric(10, 4),     -- approximate, from configured pricing constants
    created_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS call_traces_call_id_idx ON call_traces (call_id);

-- One row per eval suite run (for trend across runs).
CREATE TABLE IF NOT EXISTS eval_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at          timestamptz NOT NULL DEFAULT now(),
    git_sha         text,
    model           text,
    n_scenarios     int,
    n_passed        int,
    success_rate    numeric(5, 2),
    avg_response_ms int,
    p50_ms          int,
    p95_ms          int,
    notes           text
);

-- One row per scenario within a run (drill-down).
CREATE TABLE IF NOT EXISTS eval_results (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      uuid NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
    scenario_id text,
    name        text,
    passed      boolean,
    expected    text,
    actual      text,
    latency_ms  int,
    tool_calls  text,                          -- JSON-encoded list of tools called
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS eval_results_run_id_idx ON eval_results (run_id);

-- RLS: authenticated read-only (the dashboard's anon key acts as authenticated
-- post-login); the service role used by the agent / eval runner bypasses RLS.
ALTER TABLE turn_metrics ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated_select_turn_metrics" ON turn_metrics FOR SELECT TO authenticated USING (true);

ALTER TABLE call_traces ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated_select_call_traces" ON call_traces FOR SELECT TO authenticated USING (true);

ALTER TABLE eval_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated_select_eval_runs" ON eval_runs FOR SELECT TO authenticated USING (true);

ALTER TABLE eval_results ENABLE ROW LEVEL SECURITY;
CREATE POLICY "authenticated_select_eval_results" ON eval_results FOR SELECT TO authenticated USING (true);
