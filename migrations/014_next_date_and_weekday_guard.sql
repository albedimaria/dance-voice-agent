-- 014: le date le calcola Postgres, non l'LLM.
--
-- Trovato da un test di copertura (2026-10-19): l'agente, a lunedì 19 ottobre, prenotava
-- "martedì 21 ottobre" (p_date 2026-10-21, che è un mercoledì). L'LLM sbaglia l'aritmetica
-- delle date, e il prompt live non ha nemmeno la data di oggi.
--
-- 1. api_get_courses restituisce per ogni corso `next_date` (YYYY-MM-DD) e `next_date_label`
--    ("martedì 20 ottobre"), calcolate in Europe/Rome. Se il corso è oggi ma è già iniziato,
--    vale la settimana dopo. L'agente copia, non calcola.
-- 2. api_create_booking e api_create_trial_session rifiutano una data che non cade nel giorno
--    del corso, e nell'errore dicono la data giusta. Anche se l'LLM sbaglia, il DB no.

CREATE OR REPLACE FUNCTION public.course_next_date(p_day_of_week int, p_time_start time)
RETURNS date
LANGUAGE sql STABLE
SET search_path TO 'public'
AS $$
    WITH n AS (SELECT (now() AT TIME ZONE 'Europe/Rome') AS ts)
    SELECT (n.ts::date + (
        CASE
            WHEN ((p_day_of_week - (extract(isodow FROM n.ts)::int - 1) + 7) % 7) = 0
                 AND p_time_start <= n.ts::time THEN 7
            ELSE (p_day_of_week - (extract(isodow FROM n.ts)::int - 1) + 7) % 7
        END))::date
    FROM n
$$;

CREATE OR REPLACE FUNCTION public.date_label_it(p_date date)
RETURNS text
LANGUAGE sql IMMUTABLE
AS $$
    SELECT (ARRAY['lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica'])[extract(isodow FROM p_date)::int]
           || ' ' || extract(day FROM p_date)::int || ' ' ||
           (ARRAY['gennaio','febbraio','marzo','aprile','maggio','giugno','luglio','agosto',
                  'settembre','ottobre','novembre','dicembre'])[extract(month FROM p_date)::int]
$$;

CREATE OR REPLACE FUNCTION public.api_get_courses(p_style text DEFAULT NULL::text, p_level text DEFAULT NULL::text, p_location text DEFAULT NULL::text, p_instructor text DEFAULT NULL::text, p_day text DEFAULT NULL::text, p_caller_level text DEFAULT NULL::text)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE
    v_style text;
    v_day int;
    v_cap int := 3;   -- oltre 3 corsi non si elencano a voce: l'agente sintetizza
    v_res json;
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

    WITH matched AS (
        SELECT c.id, c.name, c.style, c.level::text AS level, c.instructor,
               c.day_of_week,
               (ARRAY['lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica'])[c.day_of_week + 1] AS day_name,
               c.time_start, c.duration_minutes, c.max_capacity, c.location,
               course_next_date(c.day_of_week, c.time_start) AS next_date
        FROM courses c
        WHERE c.active = true
          AND (coalesce(v_style, '') = '' OR c.style ILIKE '%'||v_style||'%' OR c.name ILIKE '%'||v_style||'%')
          AND (coalesce(p_level, '') = '' OR c.level::text = p_level)
          AND (coalesce(p_location, '') = '' OR c.location ILIKE '%'||p_location||'%')
          AND (coalesce(p_instructor, '') = '' OR c.instructor ILIKE '%'||p_instructor||'%')
          AND (v_day IS NULL OR c.day_of_week = v_day)
    ),
    counted AS (SELECT count(*)::int AS total FROM matched),
    shown AS (
        SELECT id, name, style, level, instructor, day_of_week, day_name,
               time_start, duration_minutes, max_capacity, location,
               next_date, date_label_it(next_date) AS next_date_label
        FROM matched
        ORDER BY
            (coalesce(p_caller_level, '') <> '' AND level = p_caller_level) DESC,
            day_of_week, time_start
        LIMIT v_cap
    )
    SELECT json_build_object(
        'total', c.total,
        'truncated', c.total > v_cap,
        'today', date_label_it((now() AT TIME ZONE 'Europe/Rome')::date),
        'courses', coalesce((SELECT json_agg(row_to_json(s)) FROM shown s), '[]'::json),
        'available_days', coalesce(
            (SELECT json_agg(d.day_name ORDER BY d.day_of_week)
             FROM (SELECT DISTINCT day_of_week, day_name FROM matched) d), '[]'::json),
        'available_levels', coalesce(
            (SELECT json_agg(DISTINCT level) FROM matched), '[]'::json),
        'available_styles', coalesce(
            (SELECT json_agg(DISTINCT style) FROM matched), '[]'::json)
    ) INTO v_res
    FROM counted c;

    RETURN v_res;
END $function$;

-- Guardia condivisa: la data deve cadere nel giorno del corso.
CREATE OR REPLACE FUNCTION public.course_date_error(p_course_id uuid, p_date date)
RETURNS text
LANGUAGE sql STABLE
SET search_path TO 'public'
AS $$
    SELECT CASE
        WHEN c.id IS NULL THEN NULL
        WHEN extract(isodow FROM p_date)::int - 1 <> c.day_of_week THEN
            'Il ' || date_label_it(p_date) || ' non c''è questo corso: è di '
            || (ARRAY['lunedì','martedì','mercoledì','giovedì','venerdì','sabato','domenica'])[c.day_of_week + 1]
            || '. La prossima lezione è ' || date_label_it(course_next_date(c.day_of_week, c.time_start))
            || ' (' || course_next_date(c.day_of_week, c.time_start) || ').'
        ELSE NULL END
    FROM (SELECT 1) one LEFT JOIN courses c ON c.id = p_course_id
$$;

CREATE OR REPLACE FUNCTION public.api_create_booking(p_student_id uuid, p_course_id uuid, p_date date)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE v_name text; v_id uuid; v_err text;
BEGIN
    IF p_date < current_date THEN RETURN json_build_object('error', 'Non puoi prenotare per una data già passata.'); END IF;
    IF p_date > current_date + 180 THEN RETURN json_build_object('error', 'Non puoi prenotare con più di 6 mesi di anticipo.'); END IF;
    SELECT name INTO v_name FROM courses WHERE id = p_course_id;
    IF NOT FOUND THEN RETURN json_build_object('error', 'Corso non trovato.'); END IF;
    v_err := course_date_error(p_course_id, p_date);
    IF v_err IS NOT NULL THEN RETURN json_build_object('error', v_err); END IF;
    BEGIN
        INSERT INTO bookings (student_id, course_id, date, type, status)
        VALUES (p_student_id, p_course_id, p_date, 'regular', 'confirmed') RETURNING id INTO v_id;
    EXCEPTION WHEN others THEN
        IF SQLERRM LIKE '%capacity_exceeded%' THEN RETURN json_build_object('error', 'Il corso "'||v_name||'" è al completo per il '||p_date||'.');
        ELSIF SQLERRM LIKE '%bookings_no_duplicate_confirmed%' THEN RETURN json_build_object('error', 'Hai già una prenotazione per questo corso in questa data.');
        ELSE RETURN json_build_object('error', SQLERRM); END IF;
    END;
    RETURN json_build_object('booking_id', v_id, 'course_name', v_name, 'date', p_date,
                             'date_label', date_label_it(p_date), 'confirmed', true);
END $function$;

CREATE OR REPLACE FUNCTION public.api_create_trial_session(p_student_id uuid, p_course_id uuid, p_date date)
 RETURNS json
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
DECLARE v_id uuid; v_err text;
BEGIN
    IF p_date < current_date THEN RETURN json_build_object('error', 'Non puoi prenotare per una data già passata.'); END IF;
    v_err := course_date_error(p_course_id, p_date);
    IF v_err IS NOT NULL THEN RETURN json_build_object('error', v_err); END IF;
    BEGIN
        INSERT INTO trial_sessions (student_id, course_id, date)
        VALUES (p_student_id, p_course_id, p_date) RETURNING id INTO v_id;
    EXCEPTION WHEN others THEN
        IF lower(SQLERRM) LIKE '%unique%' THEN RETURN json_build_object('error', 'Lo studente ha già usato la lezione di prova per questo corso.');
        ELSE RETURN json_build_object('error', SQLERRM); END IF;
    END;
    RETURN json_build_object('trial_id', v_id, 'date', p_date, 'date_label', date_label_it(p_date), 'confirmed', true);
END $function$;
