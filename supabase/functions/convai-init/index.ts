// Conversation-initiation webhook for ElevenLabs Agents.
//
// ElevenLabs calls this BEFORE the call is answered, passing the caller's number.
// We look the student up and hand back dynamic variables, so the agent already
// knows who is on the line when it says hello — no mid-conversation
// "come ti chiami?", and one fewer tool round-trip in the hot path.
//
// This replaces what main.py used to do in the old custom pipeline. After the
// migration to ConvAI nothing was doing it, which is why the agent kept asking
// for the caller's name halfway through the conversation.
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
  };

  try {
    // `settings` is a key/value table, not a column-per-flag one.
    const s = await db("settings?select=value&key=eq.trial_week_active&limit=1");
    if (s.ok) {
      const [row] = await s.json();
      vars.trial_week_active = String(row?.value ?? "false");
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

  // Only override the greeting when we actually recognised the caller; the
  // agent's own first_message already covers the anonymous case.
  if (vars.caller_known === "true") {
    const first = vars.student_name.split(" ")[0];
    payload.conversation_config_override = {
      agent: {
        // [rapidly], not [fast]: measured on v3, [fast] changes the duration by
        // exactly 0% (it is a no-op), [rapidly] shaves ~6%.
        first_message:
          `[calorosa][rapidly] Ciao ${first}! Sono Tropicoqueta di Ritmo Tropicale. Dimmi pure!`,
      },
    };
  }

  return new Response(JSON.stringify(payload), {
    headers: { "Content-Type": "application/json" },
  });
});
