-- 008_faq_and_reminders.sql
-- FAQ knowledge base (raises contain rate: fewer escalations to the secretary)
-- + reminders_log (idempotency guard for the pre-lesson reminder cron).

-- faqs: small curated table the agent reads via the get_faq tool.
-- The LLM picks the relevant answer itself, so no keyword-matching fragility
-- against STT-garbled queries.

CREATE TABLE faqs (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    topic      text NOT NULL,           -- short label, e.g. 'parcheggio'
    question   text NOT NULL,           -- canonical phrasing of the question
    answer     text NOT NULL,           -- what the agent should say
    active     boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- reminders_log: one row per reminder actually sent. UNIQUE(kind, booking_id)
-- makes the daily cron idempotent — re-runs and overlapping schedules can't
-- text the same student twice for the same lesson.

CREATE TABLE reminders_log (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       text NOT NULL CHECK (kind IN ('booking', 'trial')),
    booking_id uuid NOT NULL,
    sent_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, booking_id)
);

-- RLS: same posture as migrations 005/007 — authenticated may SELECT
-- (dashboard), writes go through the service role which bypasses RLS.

ALTER TABLE faqs ENABLE ROW LEVEL SECURITY;
ALTER TABLE reminders_log ENABLE ROW LEVEL SECURITY;

CREATE POLICY "faqs_select_authenticated"
    ON faqs FOR SELECT TO authenticated USING (true);
CREATE POLICY "reminders_log_select_authenticated"
    ON reminders_log FOR SELECT TO authenticated USING (true);

-- Seed FAQ (anonymized demo content — the school replaces these with real answers).

INSERT INTO faqs (topic, question, answer) VALUES
    ('orari_segreteria', 'Quando è aperta la segreteria?',
     'La segreteria è aperta dal lunedì al venerdì dalle 16:00 alle 21:00, presso lo Studio AIDA in Via Roma 10.'),
    ('parcheggio', 'C''è parcheggio vicino alle sedi?',
     'Vicino allo Studio AIDA c''è un parcheggio pubblico gratuito a 100 metri. Per lo Studio TIGER c''è parcheggio in strada, gratuito dopo le 19:00.'),
    ('pagamenti', 'Come si può pagare l''abbonamento?',
     'Si può pagare in contanti o con carta in segreteria, oppure con bonifico. Il pagamento si gestisce sempre con la segreteria, non al telefono.'),
    ('lezioni_private', 'Fate lezioni private?',
     'Sì, gli istruttori offrono lezioni private su prenotazione. I prezzi e le disponibilità li gestisce la segreteria: lascio volentieri una nota per farti richiamare.'),
    ('abbigliamento', 'Come bisogna vestirsi? Servono scarpe da ballo?',
     'Abbigliamento comodo. Per i corsi base vanno bene scarpe pulite da usare solo in sala; le scarpe da ballo sono consigliate dal livello intermedio in su.'),
    ('eta_minima', 'C''è un''età minima per iscriversi?',
     'I corsi per adulti partono dai 16 anni. Per i più piccoli al momento non ci sono corsi dedicati.'),
    ('serate_eventi', 'Organizzate serate o eventi?',
     'Sì, circa una volta al mese la scuola organizza una serata sociale aperta a tutti gli iscritti. Le date vengono annunciate in sala e sul gruppo WhatsApp della scuola.'),
    ('partner', 'Serve venire in coppia?',
     'No, non serve il partner: nelle lezioni si ruota regolarmente, quindi si può venire tranquillamente da soli.');
