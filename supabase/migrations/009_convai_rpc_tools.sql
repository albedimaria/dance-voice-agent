-- 009_convai_rpc_tools.sql
-- Tool layer for the ElevenLabs Conversational AI migration: the agent's server
-- tools call these Postgres functions directly via PostgREST (/rest/v1/rpc/<fn>),
-- so the business logic lives in the DB — no application server.
-- Ported from tools/supabase_tools.py. SECURITY DEFINER + granted to anon so the
-- EL agent (with the anon key) can call them; each function enforces its own rules.
-- The capacity trigger (migration 004) still fires on booking inserts.
--
-- ⚠️ HARDENING TODO: anon-key access means anyone with the key + a student_id can
-- call the write RPCs. Same trust model as the current agent (which trusts the
-- caller-derived student_id), but before real production add a signed secret /
-- dedicated scoped role checked inside these functions.

-- get_courses: filtered course search with phonetic-style normalization + IT day names.
CREATE OR REPLACE FUNCTION api_get_courses(
    p_style text DEFAULT NULL, p_level text DEFAULT NULL,
    p_location text DEFAULT NULL, p_instructor text DEFAULT NULL, p_day text DEFAULT NULL
) RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_style text; v_day int; v_res json;
BEGIN
    v_style := lower(coalesce(p_style, ''));
    v_style := replace(v_style, 'baciata', 'bachata');
    v_style := replace(v_style, 'bachiata', 'bachata');
    v_style := replace(v_style, 'facciata', 'bachata');
    v_style := replace(v_style, 'merenghe', 'merengue');
    v_style := replace(v_style, 'regghetòn', 'reggaeton');
    v_style := replace(v_style, 'reggeton', 'reggaeton');
    v_day := CASE lower(coalesce(p_day, ''))
        WHEN 'lun' THEN 0 WHEN 'lunedì' THEN 0 WHEN 'lunedi' THEN 0
        WHEN 'mar' THEN 1 WHEN 'martedì' THEN 1 WHEN 'martedi' THEN 1
        WHEN 'mer' THEN 2 WHEN 'mercoledì' THEN 2 WHEN 'mercoledi' THEN 2
        WHEN 'gio' THEN 3 WHEN 'giovedì' THEN 3 WHEN 'giovedi' THEN 3
        WHEN 'ven' THEN 4 WHEN 'venerdì' THEN 4 WHEN 'venerdi' THEN 4
        WHEN 'sab' THEN 5 WHEN 'sabato' THEN 5
        WHEN 'dom' THEN 6 WHEN 'domenica' THEN 6
        ELSE NULL END;
    SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) INTO v_res FROM (
        SELECT c.id, c.name, c.style, c.level::text AS level, c.instructor, c.day_of_week,
            (ARRAY['lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica'])[c.day_of_week + 1] AS day_name,
            c.time_start, c.duration_minutes, c.max_capacity, c.location
        FROM courses c
        WHERE c.active = true
          AND (coalesce(v_style, '') = '' OR c.style ILIKE '%'||v_style||'%' OR c.name ILIKE '%'||v_style||'%')
          AND (coalesce(p_level, '') = '' OR c.level::text = p_level)
          AND (coalesce(p_location, '') = '' OR c.location ILIKE '%'||p_location||'%')
          AND (coalesce(p_instructor, '') = '' OR c.instructor ILIKE '%'||p_instructor||'%')
          AND (v_day IS NULL OR c.day_of_week = v_day)
        ORDER BY c.day_of_week, c.time_start
    ) t;
    RETURN v_res;
END $$;

-- get_faq
CREATE OR REPLACE FUNCTION api_get_faq(p_topic text DEFAULT NULL)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_res json;
BEGIN
    SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) INTO v_res FROM (
        SELECT topic, question, answer FROM faqs
        WHERE active = true
          AND (coalesce(p_topic, '') = '' OR topic ILIKE '%'||p_topic||'%' OR question ILIKE '%'||p_topic||'%')
    ) t;
    RETURN v_res;
END $$;

-- get_student_bookings: future confirmed bookings (regular + recovery)
CREATE OR REPLACE FUNCTION api_get_student_bookings(p_student_id uuid)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_res json;
BEGIN
    SELECT coalesce(json_agg(row_to_json(t)), '[]'::json) INTO v_res FROM (
        SELECT b.id AS booking_id, b.date, b.type,
               c.name AS course_name, c.time_start, c.location
        FROM bookings b JOIN courses c ON c.id = b.course_id
        WHERE b.student_id = p_student_id AND b.status = 'confirmed' AND b.date >= current_date
        ORDER BY b.date
    ) t;
    RETURN v_res;
END $$;

-- create_booking (capacity enforced by trigger from migration 004)
CREATE OR REPLACE FUNCTION api_create_booking(p_student_id uuid, p_course_id uuid, p_date date)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_name text; v_id uuid;
BEGIN
    IF p_date < current_date THEN RETURN json_build_object('error', 'Non puoi prenotare per una data già passata.'); END IF;
    IF p_date > current_date + 180 THEN RETURN json_build_object('error', 'Non puoi prenotare con più di 6 mesi di anticipo.'); END IF;
    SELECT name INTO v_name FROM courses WHERE id = p_course_id;
    IF NOT FOUND THEN RETURN json_build_object('error', 'Corso non trovato.'); END IF;
    BEGIN
        INSERT INTO bookings (student_id, course_id, date, type, status)
        VALUES (p_student_id, p_course_id, p_date, 'regular', 'confirmed') RETURNING id INTO v_id;
    EXCEPTION WHEN others THEN
        IF SQLERRM LIKE '%capacity_exceeded%' THEN RETURN json_build_object('error', 'Il corso "'||v_name||'" è al completo per il '||p_date||'.');
        ELSIF SQLERRM LIKE '%bookings_no_duplicate_confirmed%' THEN RETURN json_build_object('error', 'Hai già una prenotazione per questo corso in questa data.');
        ELSE RETURN json_build_object('error', SQLERRM); END IF;
    END;
    RETURN json_build_object('booking_id', v_id, 'course_name', v_name, 'date', p_date, 'confirmed', true);
END $$;

-- cancel_booking (ownership/status/date checks; atomic status guard)
CREATE OR REPLACE FUNCTION api_cancel_booking(p_booking_id uuid, p_student_id uuid)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_b bookings%ROWTYPE; v_name text;
BEGIN
    SELECT * INTO v_b FROM bookings WHERE id = p_booking_id;
    IF NOT FOUND THEN RETURN json_build_object('error', 'Prenotazione non trovata.'); END IF;
    IF v_b.student_id <> p_student_id THEN RETURN json_build_object('error', 'Questa prenotazione non appartiene allo studente.'); END IF;
    IF v_b.status <> 'confirmed' THEN RETURN json_build_object('error', 'La prenotazione risulta già annullata.'); END IF;
    IF v_b.date < current_date THEN RETURN json_build_object('error', 'Non si può annullare una lezione già passata.'); END IF;
    UPDATE bookings SET status = 'cancelled' WHERE id = p_booking_id AND status = 'confirmed';
    SELECT name INTO v_name FROM courses WHERE id = v_b.course_id;
    RETURN json_build_object('cancelled', true, 'course_name', v_name, 'date', v_b.date);
END $$;

-- create_recovery (level rules + capacity)
CREATE OR REPLACE FUNCTION api_create_recovery(p_student_id uuid, p_course_id uuid, p_date date)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_slevel text; v_clevel text; v_allowed text[]; v_name text; v_id uuid;
BEGIN
    IF p_date < current_date THEN RETURN json_build_object('error', 'Non puoi prenotare per una data già passata.'); END IF;
    IF p_date > current_date + 180 THEN RETURN json_build_object('error', 'Non puoi prenotare con più di 6 mesi di anticipo.'); END IF;
    SELECT level::text INTO v_slevel FROM students WHERE id = p_student_id;
    IF NOT FOUND THEN RETURN json_build_object('error', 'Studente non trovato.'); END IF;
    SELECT level::text, name INTO v_clevel, v_name FROM courses WHERE id = p_course_id;
    IF NOT FOUND THEN RETURN json_build_object('error', 'Corso non trovato.'); END IF;
    v_allowed := CASE v_slevel
        WHEN 'intermedio' THEN ARRAY['base']
        WHEN 'avanzato' THEN ARRAY['intermedio','base']
        ELSE ARRAY[]::text[] END;
    IF NOT (v_clevel = ANY(v_allowed)) THEN
        IF array_length(v_allowed, 1) IS NULL THEN
            RETURN json_build_object('error', 'Gli studenti di livello '||v_slevel||' non possono fare recuperi.');
        ELSE
            RETURN json_build_object('error', 'Livello incompatibile: uno studente '||v_slevel||' può recuperare solo in corsi '||array_to_string(v_allowed, ' o ')||'.');
        END IF;
    END IF;
    BEGIN
        INSERT INTO bookings (student_id, course_id, date, type, status)
        VALUES (p_student_id, p_course_id, p_date, 'recovery', 'confirmed') RETURNING id INTO v_id;
    EXCEPTION WHEN others THEN
        IF SQLERRM LIKE '%capacity_exceeded%' THEN RETURN json_build_object('error', 'Il corso "'||v_name||'" è al completo per il '||p_date||'.');
        ELSIF SQLERRM LIKE '%bookings_no_duplicate_confirmed%' THEN RETURN json_build_object('error', 'Hai già un recupero per questo corso in questa data.');
        ELSE RETURN json_build_object('error', SQLERRM); END IF;
    END;
    RETURN json_build_object('booking_id', v_id, 'course_name', v_name, 'date', p_date, 'confirmed', true);
END $$;

-- check_trial_used
CREATE OR REPLACE FUNCTION api_check_trial_used(p_student_id uuid, p_course_id uuid)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    RETURN json_build_object('used', EXISTS(
        SELECT 1 FROM trial_sessions WHERE student_id = p_student_id AND course_id = p_course_id));
END $$;

-- create_trial_session
CREATE OR REPLACE FUNCTION api_create_trial_session(p_student_id uuid, p_course_id uuid, p_date date)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_id uuid;
BEGIN
    IF p_date < current_date THEN RETURN json_build_object('error', 'Non puoi prenotare per una data già passata.'); END IF;
    BEGIN
        INSERT INTO trial_sessions (student_id, course_id, date)
        VALUES (p_student_id, p_course_id, p_date) RETURNING id INTO v_id;
    EXCEPTION WHEN others THEN
        IF lower(SQLERRM) LIKE '%unique%' THEN RETURN json_build_object('error', 'Lo studente ha già usato la lezione di prova per questo corso.');
        ELSE RETURN json_build_object('error', SQLERRM); END IF;
    END;
    RETURN json_build_object('trial_id', v_id, 'date', p_date, 'confirmed', true);
END $$;

-- get_pricing (pure)
CREATE OR REPLACE FUNCTION api_get_pricing(p_course_count int)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_total int;
BEGIN
    IF p_course_count <= 0 THEN RETURN json_build_object('error', 'Il numero di corsi deve essere almeno 1.'); END IF;
    v_total := 160 + (p_course_count - 1) * 128;
    RETURN json_build_object('total', v_total, 'currency', 'EUR', 'course_count', p_course_count,
        'note', 'Primo corso 160€, ogni corso aggiuntivo 128€ (−20%).');
END $$;

-- get_settings
CREATE OR REPLACE FUNCTION api_get_settings()
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v_res json;
BEGIN
    SELECT coalesce(json_object_agg(key, value), '{}'::json) INTO v_res FROM settings;
    RETURN v_res;
END $$;

-- Expose to the anon role (the key the EL agent will use).
GRANT EXECUTE ON FUNCTION
    api_get_courses(text,text,text,text,text), api_get_faq(text),
    api_get_student_bookings(uuid), api_create_booking(uuid,uuid,date),
    api_cancel_booking(uuid,uuid), api_create_recovery(uuid,uuid,date),
    api_check_trial_used(uuid,uuid), api_create_trial_session(uuid,uuid,date),
    api_get_pricing(int), api_get_settings()
TO anon, authenticated;
