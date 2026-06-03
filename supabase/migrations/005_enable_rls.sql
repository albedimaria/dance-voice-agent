-- 005_enable_rls.sql
--
-- Enable Row Level Security on tables that contain PII or are writable
-- via the dashboard. The voice agent uses SUPABASE_SERVICE_ROLE_KEY, which
-- bypasses RLS entirely, so these policies only affect anon / authenticated
-- connections such as the Next.js dashboard.
--
-- Access model:
--   anon key (public/unauthenticated) → blocked on all four tables
--   authenticated (logged-in dashboard user) → SELECT on all, UPDATE on settings

-- ─── students ────────────────────────────────────────────────────────────────

ALTER TABLE students ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_select_students"
    ON students
    FOR SELECT
    TO authenticated
    USING (true);

-- ─── call_logs ───────────────────────────────────────────────────────────────

ALTER TABLE call_logs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_select_call_logs"
    ON call_logs
    FOR SELECT
    TO authenticated
    USING (true);

-- ─── bookings ────────────────────────────────────────────────────────────────

ALTER TABLE bookings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_select_bookings"
    ON bookings
    FOR SELECT
    TO authenticated
    USING (true);

-- ─── settings ────────────────────────────────────────────────────────────────

ALTER TABLE settings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_select_settings"
    ON settings
    FOR SELECT
    TO authenticated
    USING (true);

-- The dashboard trial-toggle writes settings.value via the browser client.
CREATE POLICY "authenticated_update_settings"
    ON settings
    FOR UPDATE
    TO authenticated
    USING (true)
    WITH CHECK (true);

-- ─── courses ─────────────────────────────────────────────────────────────────
-- Non-sensitive but must have RLS enabled to satisfy Supabase security checks.
-- Voice agent reads via service_role (bypasses RLS); dashboard reads as authenticated.

ALTER TABLE courses ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_select_courses"
    ON courses
    FOR SELECT
    TO authenticated
    USING (true);

-- ─── trial_sessions ──────────────────────────────────────────────────────────

ALTER TABLE trial_sessions ENABLE ROW LEVEL SECURITY;

CREATE POLICY "authenticated_select_trial_sessions"
    ON trial_sessions
    FOR SELECT
    TO authenticated
    USING (true);
