-- 010_get_courses_summary.sql
-- api_get_courses smette di restituire l'elenco piatto dei corsi.
--
-- PERCHÉ. La v1 restituiva un array con TUTTI i corsi che passavano il filtro:
-- senza filtri sono quattordici. L'agente se li ritrovava in contesto e li
-- leggeva al telefono, dove una lista non si ascolta — si subisce. La cura era
-- una regola nel prompt ("non leggere MAI una lista", nove righe di divieti):
-- si chiedeva al modello di ignorare quello che gli avevamo appena messo in
-- mano. Obbedienza misurata: 1 caso su 3.
--
-- Ora il dato è già la risposta:
--   total <= 2  -> courses = tutti (uno lo dici, due li dici in una frase)
--   total >= 3  -> courses = UN solo corso (il primo della settimana),
--                  truncated = true, più le sfaccettature (giorni/livelli/stili
--                  disponibili) per fare LA domanda che restringe.
--
-- L'agente non può elencare quattordici corsi perché non ne ha quattordici: ne
-- ha uno e un conteggio. Il divieto diventa una proprietà della struttura dati,
-- non una speranza riposta nel modello.
--
-- Compatibilità: l'unico consumatore è il server tool `get_courses` dell'agente
-- ConvAI (PostgREST /rpc/api_get_courses). La vecchia pipeline Python usa
-- tools/supabase_tools.py, che interroga le tabelle e non questa RPC.
-- Rollback: rilanciare la definizione che sta in 009_convai_rpc_tools.sql.
--
-- Nota implementativa: tutto in una CTE, niente tabelle temporanee. In una
-- funzione SECURITY DEFINER con search_path bloccato su public lo schema
-- temporaneo non è nel path, e un `CREATE TEMP TABLE` si rivelerebbe al primo
-- caller, non qui.

CREATE OR REPLACE FUNCTION api_get_courses(
    p_style text DEFAULT NULL, p_level text DEFAULT NULL,
    p_location text DEFAULT NULL, p_instructor text DEFAULT NULL, p_day text DEFAULT NULL
) RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_style text;
    v_day int;
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
               c.time_start, c.duration_minutes, c.max_capacity, c.location
        FROM courses c
        WHERE c.active = true
          AND (coalesce(v_style, '') = '' OR c.style ILIKE '%'||v_style||'%' OR c.name ILIKE '%'||v_style||'%')
          AND (coalesce(p_level, '') = '' OR c.level::text = p_level)
          AND (coalesce(p_location, '') = '' OR c.location ILIKE '%'||p_location||'%')
          AND (coalesce(p_instructor, '') = '' OR c.instructor ILIKE '%'||p_instructor||'%')
          AND (v_day IS NULL OR c.day_of_week = v_day)
    ),
    counted AS (SELECT count(*)::int AS total FROM matched),
    -- Tre o più risultati: l'agente ne riceve UNO. Non è una troncatura per
    -- risparmiare token: è ciò che gli impedisce di elencare.
    shown AS (
        SELECT * FROM matched
        ORDER BY day_of_week, time_start
        LIMIT (SELECT CASE WHEN total >= 3 THEN 1 ELSE total END FROM counted)
    )
    SELECT json_build_object(
        'total', c.total,
        'truncated', c.total >= 3,
        'courses', coalesce((SELECT json_agg(row_to_json(s)) FROM shown s), '[]'::json),
        'available_days', CASE WHEN c.total >= 3 THEN coalesce(
            (SELECT json_agg(d.day_name ORDER BY d.day_of_week)
             FROM (SELECT DISTINCT day_of_week, day_name FROM matched) d), '[]'::json)
            ELSE '[]'::json END,
        'available_levels', CASE WHEN c.total >= 3 THEN coalesce(
            (SELECT json_agg(DISTINCT level) FROM matched), '[]'::json)
            ELSE '[]'::json END,
        'available_styles', CASE WHEN c.total >= 3 THEN coalesce(
            (SELECT json_agg(DISTINCT style) FROM matched), '[]'::json)
            ELSE '[]'::json END
    ) INTO v_res
    FROM counted c;

    RETURN v_res;
END $$;
