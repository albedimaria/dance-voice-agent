// secretary-report — report settimanale via EMAIL alla SEGRETERIA (solo scuola, non clienti).
// Aggrega le chiamate degli ultimi 7 giorni da call_logs e le manda in una mail.
// Pensata per girare da pg_cron (una volta a settimana) o su chiamata manuale.
//
// GATE (lato Alberto): account Resend + dominio mittente (SPF/DKIM).
// Secret Supabase: RESEND_API_KEY, SECRETARY_EMAIL, REPORT_FROM_EMAIL (es. report@tuodominio.it).
// SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY sono già iniettati nell'Edge runtime.
//
// Deploy: supabase functions deploy secretary-report

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const SB_URL = Deno.env.get("SUPABASE_URL") ?? "";
const SB_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") ?? "";
const RESEND = Deno.env.get("RESEND_API_KEY") ?? "";
const TO = Deno.env.get("SECRETARY_EMAIL") ?? "";
const FROM = Deno.env.get("REPORT_FROM_EMAIL") ?? "";

serve(async () => {
  if (!RESEND || !TO || !FROM) {
    return new Response(JSON.stringify({ error: "Resend/email non configurati (secret mancanti)" }),
      { status: 503, headers: { "Content-Type": "application/json" } });
  }
  const since = new Date(Date.now() - 7 * 864e5).toISOString();
  // Read the week's calls (service role bypasses RLS).
  const res = await fetch(
    `${SB_URL}/rest/v1/call_logs?select=intent_detected,outcome,escalated,duration_seconds&created_at=gte.${since}`,
    { headers: { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` } },
  );
  const rows: Array<{ intent_detected: string; outcome: string; escalated: boolean; duration_seconds: number }> =
    await res.json().catch(() => []);

  const n = rows.length;
  const by = (k: string) => rows.filter((r) => r.intent_detected === k).length;
  const esc = rows.filter((r) => r.escalated).length;
  const avgMin = n ? (rows.reduce((a, r) => a + (r.duration_seconds || 0), 0) / n / 60).toFixed(1) : "0";

  const html = `
    <h2>Report settimanale — assistente vocale Ritmo Tropicale</h2>
    <p>Ultimi 7 giorni.</p>
    <ul>
      <li><b>${n}</b> chiamate gestite</li>
      <li><b>${by("prenotazione")}</b> prenotazioni · <b>${by("recupero")}</b> recuperi · <b>${by("disdetta")}</b> disdette</li>
      <li><b>${by("info_corsi")}</b> richieste info · <b>${by("faq")}</b> FAQ</li>
      <li><b>${esc}</b> passate a una persona</li>
      <li>durata media: <b>${avgMin} min</b></li>
    </ul>
    <p style="color:#888;font-size:12px">Generato automaticamente. Rispondi a questa mail per segnalare correzioni.</p>`;

  const send = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${RESEND}`, "Content-Type": "application/json" },
    body: JSON.stringify({
      from: FROM, to: [TO],
      subject: `Report settimanale · ${n} chiamate`, html,
    }),
  });
  const out = await send.json().catch(() => ({}));
  return new Response(JSON.stringify({ ok: send.ok, calls: n, resend: out }),
    { status: send.ok ? 200 : 502, headers: { "Content-Type": "application/json" } });
});
