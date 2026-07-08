-- 010_convai_get_student_by_phone.sql
-- Caller identification RPC for the ConvAI agent: the agent calls this first with
-- the caller's number (EL system__caller_id) to recognize the student.
CREATE OR REPLACE FUNCTION api_get_student_by_phone(p_phone text)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v json;
BEGIN
    SELECT row_to_json(t) INTO v FROM (
        SELECT id AS student_id, first_name, last_name, level::text AS level,
               active_subscription, language_preference::text AS language_preference
        FROM students WHERE phone = p_phone
    ) t;
    RETURN coalesce(v, json_build_object('found', false));
END $$;
GRANT EXECUTE ON FUNCTION api_get_student_by_phone(text) TO anon, authenticated;
