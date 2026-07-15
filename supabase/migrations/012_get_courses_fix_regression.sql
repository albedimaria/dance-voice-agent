-- 012_get_courses_fix_regression.sql
-- Ripara la regressione introdotta da 010.
--
-- COSA ERA SUCCESSO. 010 restituiva UN solo corso quando i risultati erano 3+,
-- per impedire all'agente di "leggere le liste". Effetto collaterale, misurato su
-- una telefonata vera (2026-07-15, 21 turni): al chiamante che chiedeva "dammi i
-- corsi di bachata sensual e dimmi che giorni ci sono" — che sono TRE — l'agente
-- ne riceveva UNO e non poteva rispondere. Da lì un giro a vuoto: quattro
-- ricerche, "ti cerco i corsi di sensual" ripetuto quattro volte, il chiamante
-- furioso. Il prompt non poteva salvarlo: la risposta che serviva (elencare tre
-- corsi) era resa impossibile dal tool.
--
-- COSA FA ORA.
--   * courses = fino a 6 corsi VERI (non uno): l'agente ha di che rispondere.
--     Tre corsi di sensual si dicono; nessuno stile reale supera i 7, "bachata"
--     generico ne matcha ~12 ed è l'unico caso in cui il cap morde.
--   * total = il conteggio vero, sempre. Il prompt decide se elencarli (pochi) o
--     sintetizzarli (tanti): è una decisione linguistica, non del tool.
--   * p_caller_level (NUOVO, opzionale): se il chiamante ha un livello noto, i
--     corsi di quel livello vengono ORDINATI PER PRIMI. Non filtra, non esclude
--     niente — così a un avanzato si propone prima l'avanzato, ma se chiede
--     esplicitamente il base lo trova comunque. Risolve l'altro difetto della
--     stessa telefonata: a un avanzato veniva proposto il corso base.
--   * available_days/levels/styles restano, utili quando i risultati sono tanti.
--
-- Rollback: rilanciare la definizione in 009 (lista piena, nessun cap).

DROP FUNCTION IF EXISTS api_get_courses(text, text, text, text, text);

CREATE OR REPLACE FUNCTION api_get_courses(
    p_style text DEFAULT NULL, p_level text DEFAULT NULL,
    p_location text DEFAULT NULL, p_instructor text DEFAULT NULL, p_day text DEFAULT NULL,
    p_caller_level text DEFAULT NULL
) RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
    v_style text;
    v_day int;
    v_cap int := 6;   -- oltre questo non si elenca a voce; il prompt sintetizza
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
    shown AS (
        SELECT id, name, style, level, instructor, day_of_week, day_name,
               time_start, duration_minutes, max_capacity, location
        FROM matched
        ORDER BY
            -- il livello del chiamante viene prima, senza escludere gli altri
            (coalesce(p_caller_level, '') <> '' AND level = p_caller_level) DESC,
            day_of_week, time_start
        LIMIT v_cap
    )
    SELECT json_build_object(
        'total', c.total,
        'truncated', c.total > v_cap,
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
END $$;

GRANT EXECUTE ON FUNCTION api_get_courses(text, text, text, text, text, text) TO anon, authenticated;
