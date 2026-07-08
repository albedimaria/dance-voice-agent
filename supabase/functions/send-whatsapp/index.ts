// send-whatsapp — invia un messaggio WhatsApp al cliente via Meta Cloud API.
// Canale cliente PRIORITARIO (conferme prenotazione, reminder pre-lezione).
//
// Usato in due modi:
//  1) come server-tool dell'agente ConvAI (conferma post-prenotazione), oppure
//  2) chiamata da un DB trigger / dal cron reminder.
//
// GATE (lato Alberto, prima che funzioni):
//  - Verifica Meta Business + numero WhatsApp Business (WABA) su Meta Cloud API.
//  - Template approvati da Meta (vedi i draft nel messaggio di chat / README).
//  - Secret Supabase: META_WA_TOKEN, META_WA_PHONE_ID.
//
// Deno / Supabase Edge Function. Deploy: supabase functions deploy send-whatsapp

import { serve } from "https://deno.land/std@0.224.0/http/server.ts";

const TOKEN = Deno.env.get("META_WA_TOKEN") ?? "";
const PHONE_ID = Deno.env.get("META_WA_PHONE_ID") ?? "";

// E.164 senza '+', come vuole Meta (es. 393331234567).
function toWaNumber(phone: string): string {
  return phone.replace(/[^\d]/g, "");
}

serve(async (req) => {
  if (req.method !== "POST") return new Response("Method not allowed", { status: 405 });
  if (!TOKEN || !PHONE_ID) {
    return new Response(JSON.stringify({ error: "Meta WhatsApp non configurato (secret mancanti)" }),
      { status: 503, headers: { "Content-Type": "application/json" } });
  }

  let body: { to?: string; template?: string; lang?: string; params?: string[] };
  try { body = await req.json(); } catch { return new Response("bad json", { status: 400 }); }

  const to = body.to ? toWaNumber(body.to) : "";
  const template = body.template ?? "prenotazione_confermata";
  const lang = body.lang ?? "it";
  const params = body.params ?? [];
  if (!to) return new Response(JSON.stringify({ error: "campo 'to' mancante" }), { status: 400 });

  // Business-initiated → messaggio TEMPLATE pre-approvato (non testo libero).
  const payload = {
    messaging_product: "whatsapp",
    to,
    type: "template",
    template: {
      name: template,
      language: { code: lang },
      components: params.length
        ? [{ type: "body", parameters: params.map((t) => ({ type: "text", text: t })) }]
        : [],
    },
  };

  const res = await fetch(`https://graph.facebook.com/v21.0/${PHONE_ID}/messages`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const out = await res.json().catch(() => ({}));
  return new Response(JSON.stringify({ ok: res.ok, status: res.status, meta: out }),
    { status: res.ok ? 200 : 502, headers: { "Content-Type": "application/json" } });
});
