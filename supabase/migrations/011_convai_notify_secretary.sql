-- 011_convai_notify_secretary.sql
-- Escalation "avvisa la segreteria" per l'agente ConvAI, senza dipendere da
-- account esterni: la nota finisce in una tabella che la segreteria vede in
-- dashboard/Centralino. Il push WhatsApp/email è un layer sopra (fase 2).
CREATE TABLE IF NOT EXISTS secretary_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phone_from text,
    message text NOT NULL,
    handled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE secretary_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "sm_select_auth" ON secretary_messages;
CREATE POLICY "sm_select_auth" ON secretary_messages FOR SELECT TO authenticated USING (true);

CREATE OR REPLACE FUNCTION api_notify_secretary(p_message text, p_phone_from text DEFAULT NULL)
RETURNS json LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE v uuid;
BEGIN
    INSERT INTO secretary_messages(phone_from, message) VALUES (p_phone_from, p_message) RETURNING id INTO v;
    RETURN json_build_object('notified', true, 'id', v);
END $$;
GRANT EXECUTE ON FUNCTION api_notify_secretary(text, text) TO anon, authenticated;
