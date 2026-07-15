// Conversation-initiation webhook for ElevenLabs Agents.
//
// ElevenLabs calls this BEFORE the call is answered, passing the caller's number.
// We look the student up and hand back dynamic variables, so the agent already
// knows who is on the line when it says hello — no mid-conversation
// "come ti chiami?", and one fewer tool round-trip in the hot path.
//
// Il nome dell'agente (Karol) vive in UN solo posto: settings.agent_name nel DB.
// Il webhook lo legge e lo passa come dynamic variable {{agent_name}}, usata sia
// dal first_message del pannello (chiamanti nuovi) sia dall'override qui sotto
// (chiamanti riconosciuti). Così un cambio di nome si fa in un punto e vale
// ovunque — il bug del 2026-07-15 (nome hardcoded nel webhook che ignorava il
// cambio dal pannello) non si ripete.
//
// Auth: ElevenLabs sends the project's anon key as a Bearer token, so the
// platform's own JWT check gates this function (verify_jwt = true).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const db = (path: string, init?: RequestInit) =>
  fetch(`${SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: SERVICE_KEY,
      Authorization: `Bearer ${SERVICE_KEY}`,
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });

// Callers dial in many shapes: +39 351..., 0039351..., 351....
// Compare on the last 9 digits, which is the whole Italian subscriber number.
const tail = (s: string) => s.replace(/\D/g, "").slice(-9);

Deno.serve(async (req: Request) => {
  let callerId = "";
  try {
    const body = await req.json();
    callerId = body?.caller_id ?? body?.system__caller_id ?? "";
  } catch {
    // EL sends JSON; anything else we treat as an anonymous caller.
  }

  // Defaults for an unknown caller. Every variable MUST be present: a missing
  // dynamic variable makes ElevenLabs reject the conversation outright.
  const vars: Record<string, string> = {
    student_name: "",
    student_id: "",
    student_level: "",
    caller_known: "false",
    trial_week_active: "false",
    agent_name: "Karol",
  };

  try {
    // `settings` is a key/value table: leggiamo in un colpo trial_week_active
    // (stato prova) e agent_name (fonte unica del nome dell'agente).
    const s = await db("settings?select=key,value&key=in.(trial_week_active,agent_name)");
    if (s.ok) {
      const rows = await s.json();
      for (const row of rows) {
        if (row.key === "trial_week_active") vars.trial_week_active = String(row.value ?? "false");
        if (row.key === "agent_name" && row.value) vars.agent_name = String(row.value);
      }
    }

    if (callerId) {
      const t = tail(callerId);
      const r = await db(
        `students?select=id,first_name,last_name,level&phone=like.*${t}&limit=1`,
      );
      if (r.ok) {
        const [st] = await r.json();
        if (st) {
          vars.student_name = [st.first_name, st.last_name].filter(Boolean).join(" ");
          vars.student_id = String(st.id ?? "");
          vars.student_level = st.level ?? "";
          vars.caller_known = "true";
        }
      }
    }
  } catch (e) {
    // Never block the call on a lookup failure: fall through as an unknown
    // caller and let the agent ask, exactly as it does today.
    console.error("convai-init lookup failed", e);
  }

  const payload: Record<string, unknown> = {
    type: "conversation_initiation_client_data",
    dynamic_variables: vars,
  };

  // Chiamante riconosciuto: saluto col suo nome + presentazione PIENA col nome
  // dell'agente letto da settings. La presentazione piena serve anche a chi è in
  // rubrica: "Ciao Alberto! Sono Karol" da solo non fa capire chi risponde (il
  // chiamante chiede "ok, chi sei?"). Il nome (agent_name) non è hardcoded qui:
  // viene da settings.agent_name, la fonte unica.
  if (vars.caller_known === "true") {
    const first = vars.student_name.split(" ")[0];
    payload.conversation_config_override = {
      agent: {
        first_message:
          `Ciao ${first}! Sono ${vars.agent_name}, l'assistente automatico di Ritmo Tropicale. Dimmi pure!`,
      },
    };
  }

  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
});
