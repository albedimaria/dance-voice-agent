# dance-voice-agent

Inbound voice agent for a Latin dance school. Handles student calls autonomously — course information, lesson bookings, recovery bookings, and secretary escalation via WhatsApp. Deployed in production on Render with Twilio telephony.

The agent speaks Italian, identifies callers by phone number, and operates within strict business rules (recovery level constraints, capacity checks). All conversation state is ephemeral; persistent state lives in Supabase.

---

## Project status & privacy

This is a functional prototype, deployed and operational on Render, currently in discussion with a
real Latin-dance school for production adoption. To protect the prospective client's
confidentiality, all client-identifying details (name, addresses, phone numbers) have been replaced
with fictional placeholders throughout the code and git history. The architecture, pipeline, and
business logic are unchanged and fully representative of the real system.

---

## Architecture Overview

```
PSTN call
    │
    ▼
Twilio (inbound)
    │  mulaw 8kHz WebSocket stream
    ▼
FastAPI WebSocket handler
    ├──► Deepgram Nova-2 (streaming STT)
    │        │
    │        ├── interim transcript (non-empty) ──► barge-in: clear Twilio buffer + interrupt TTS
    │        └── is_final transcript           ──► LLM queue
    │
    ├──► GPT-4o (agentic loop)
    │        │  tool calls
    │        ├── get_student_by_phone ──► Supabase
    │        ├── get_courses          ──► Supabase  (TTL-cached, +day filter)
    │        ├── create_booking       ──► Supabase
    │        ├── create_recovery      ──► Supabase
    │        ├── notify_secretary     ──► Twilio WhatsApp API
    │        ├── get_settings         ──► Supabase
    │        ├── check_trial_used     ──► Supabase
    │        ├── create_trial_session ──► Supabase
    │        └── get_pricing          ──► (pure function, no I/O)
    │
    └──► ElevenLabs TTS (eleven_flash_v2_5, persistent WebSocket + auto_mode)
             │  mulaw 8kHz streamed chunks (ulaw_8000, Twilio-native)
             │  160-byte frames (20ms mulaw)
             ▼
         Twilio (audio out)
```

Three concurrent async tasks per call — `deepgram_sender`, `llm_worker`, `tts_sender` — communicate via asyncio queues. The main WebSocket loop feeds raw audio to Deepgram and dispatches Twilio stream events.

---

## Why Custom Pipeline vs Managed Platforms

Platforms like Vapi, Bland, or Twilio AI Assistants abstract the audio pipeline in exchange for vendor lock-in, limited control over STT/LLM/TTS choice, and higher per-minute costs (typically $0.05–0.15/min vs ~$0.005–0.015 for the equivalent custom stack).

The custom pipeline runs each component independently. Swapping any layer — STT provider, LLM model, TTS voice — requires touching only the relevant adapter (~5–10 lines), not the pipeline architecture. The WebSocket handler is ~400 lines and owns the full call lifecycle, which makes behaviour predictable and debuggable.

---

## Key Technical Decisions

**Deepgram for STT**
Deepgram Nova-2 has a native streaming WebSocket API designed for telephony (mulaw, 8kHz, 8-bit) with `interim_results` enabled. Alternatives (Whisper, Google Speech) are batch-oriented or introduce higher latency. Streaming interim transcripts are what make sub-second barge-in possible.

**Barge-in via the first interim transcript**
Deepgram is subscribed to both `SpeechStarted` (VAD) and `Transcript` events, but barge-in is driven by the first **non-empty interim transcript** while the agent is speaking — not by the raw VAD event. This is a deliberate robustness tradeoff: VAD fires on any sound (coughs, background noise, line hiss) and would cause false interruptions on a noisy phone line, whereas requiring an actual interim transcript means real speech was recognised. The cost is a small added latency (the time Deepgram needs to emit the first interim token) in exchange for far fewer false barge-ins.

When that interim transcript arrives with `is_speaking = True`: the TTS stream is abandoned mid-chunk, the `tts_queue` is drained, and a `{"event": "clear"}` message is sent to Twilio to flush the audio buffer on the caller's end. The `is_speaking` flag is checked at every 160-byte frame boundary in `tts_sender`, so once triggered the interruption is near-immediate (~20ms). A 0.8s cooldown after a barge-in prevents the tail of the agent's own audio, or the caller's continuing speech, from being mis-interpreted as a second interruption.

**Twilio-native TTS output (no conversion step)**
TTS is requested as `ulaw_8000` — mulaw at 8kHz, exactly what Twilio Media Streams expects — so chunks flow from ElevenLabs to Twilio with no resampling and no format conversion. Output is buffered and flushed in exact 160-byte frames to maintain mulaw frame alignment on the Twilio side. (Earlier versions requested PCM 24kHz and downsampled with `audioop`, which was removed from the stdlib in Python 3.13; requesting the native format deleted that entire failure mode and ~80% of the TTS bandwidth. Model choice follows the same telephony logic: `eleven_flash_v2_5` — ~75ms model latency, half the cost/char — because an 8kHz phone line physically cannot carry the extra fidelity of the expressive `eleven_v3`.)

**Persistent TTS WebSocket with per-sentence contexts**
Synthesis runs over one ElevenLabs multi-context WebSocket per call, opened when the call starts and kept warm across turns, with `auto_mode` triggering generation the moment a sentence's context closes (the deprecated `optimize_streaming_latency` knob is gone). Each sentence gets its own context id, which is what makes barge-in clean: interrupting abandons the current context, and any frames still in flight are filtered out by id instead of leaking into the next response. The socket is re-established lazily if ElevenLabs drops it during a long silence, and the switch to a native-async client also removed the old sync-generator-in-a-thread bridge. Measured with `evals/tts_bench.py` (median, n=6): TTFB 294ms → 137ms and total synthesis 1042ms → 209ms vs the legacy HTTP `eleven_v3` config — with per-request jitter that the warm connection almost eliminates (125–149ms spread).

**Stateless HMAC tokens for WebSocket auth**
Twilio's `<Stream>` injects parameters into the WebSocket `start` event. The server mints a short-lived HMAC token (30s TTL) on each `/incoming-call` request, passes it as a stream parameter, and verifies it on `start`. This prevents arbitrary WebSocket connections to `/media-stream` without requiring session storage — safe across multiple workers.

**Recovery rules encoded in Python, not in DB**
```python
RECOVERY_RULES = {
    "intermedio": ["base"],
    "avanzato":   ["intermedio", "base"],
    "base":       [],
}
```
These are stable invariants of the school's policy, not data. Encoding them in code means: they're version-controlled, the check always runs server-side regardless of what the LLM requests, and they're trivially auditable. A DB-driven approach would add a join and open a surface for data corruption.

**Call intent derived from tool calls, not LLM text**
At WebSocket close, `intent_detected` and `outcome` are derived from the set of tools actually invoked during the session — not from parsing LLM responses. Priority: `escalation > booking > recovery > course_info > unknown`. This is deterministic, manipulation-resistant, and unaffected by hallucinations.

**Phonetic normalization for STT transcription errors**
The school's dance styles have non-standard Italian pronunciations ("bachata" → spoken as "baciata"). The system prompt instructs the agent to use phonetic spellings in speech output. When the LLM calls `get_courses`, a normalization table in `tools/supabase_tools.py` maps phonetic variants back to canonical DB values before querying. This bridges the STT→LLM→DB gap without requiring the LLM to remember dual spellings.

**TTL-cached course lookup**
`get_courses` results are cached in-memory for 5 minutes per unique filter combination. The course schedule changes at most weekly; caching eliminates Supabase round-trips for repeated queries within a call (common when a caller asks multiple questions about the same style) and reduces latency from ~100–200ms to <1ms on cache hits.

---

## Agent Capabilities and Business Logic

**`get_student_by_phone`**
Lookup by caller's E.164 phone number (injected from the Twilio stream `start` event). If found, name, level, and subscription status are injected as a system message before the first user turn — the agent greets the caller by name without asking. Unrecognised numbers are handled conversationally.

**`get_courses`**
Queries `courses` filtered by `active = true`, with optional `style`, `level`, `location`, `instructor`, and `day` parameters. The `day` parameter accepts Italian day names ("lunedì", "lun", etc.) and maps them to the `day_of_week` integer in the DB. Results include a human-readable `day_name` field, removing the LLM's need to translate integers. The LLM is instructed to always call this tool before answering any course-related question — it has no fallback knowledge of the schedule.

**`create_booking`**
Inserts a `regular` booking after verifying capacity: counts confirmed bookings for the requested course/date and rejects if `confirmed >= max_capacity`. The check is also enforced at the DB level via a trigger, so concurrent bookings cannot exceed capacity even if two requests race.

**`create_recovery`**
Enforces recovery rules server-side regardless of what the LLM requests:
- Only lower-level recovery is allowed (intermedio → base; avanzato → intermedio/base)
- Base students cannot recover (no lower level exists)
- Capacity is verified before inserting
Both the level check and the capacity check happen in a single DB session to avoid race conditions.

**`notify_secretary`**
Sends a WhatsApp message via Twilio to the school's secretary number when the agent cannot resolve a request (complaints, payments, out-of-scope queries). Includes caller phone and a description. Uses `asyncio.to_thread` to wrap the synchronous Twilio client.

**`get_pricing`** (pure function, no I/O)
Calculates subscription cost: first course €160, each additional at €128 (−20%). Returns total, per-course breakdown, and a human-readable note. No DB call, no side effects.

**`get_settings` / `check_trial_used` / `create_trial_session`**
Support the trial week feature: a `settings` table flag enables free participation across all courses. Per-student, per-course trial tracking prevents duplicate free sessions.

---

## Database Schema

Six tables in Supabase PostgreSQL (eu-west-1). RLS is enabled on all tables; the server uses the service role key.

### `students`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | `gen_random_uuid()` |
| phone | text UNIQUE | E.164, lookup key from Twilio |
| first_name | text | |
| last_name | text | |
| level | enum | `base / intermedio / avanzato` |
| level_verified | boolean | false if collected during call |
| active_subscription | boolean | |
| language_preference | enum | `it / es`, default `it` |
| created_at | timestamptz | |

### `courses`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| name | text | e.g. "Salsa Base", "Bachata Intermedio" |
| style | text | `salsa / bachata / merengue / cumbia / reggaeton` |
| level | enum | `base / intermedio / avanzato` |
| instructor | text | |
| day_of_week | int | 0 = Monday … 6 = Sunday (checked: 0–6) |
| time_start | time | |
| duration_minutes | int | |
| max_capacity | int | |
| location | text nullable | `AIDA / TIGER` |
| active | boolean | false = hidden from agent |

### `bookings`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| student_id | uuid FK → students | |
| course_id | uuid FK → courses | |
| date | date | specific lesson date |
| type | enum | `regular / recovery` |
| status | enum | `confirmed / cancelled` |
| created_at | timestamptz | |

DB-level constraint: unique confirmed booking per (student, course, date); capacity trigger blocks over-booking.

### `call_logs`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| student_id | uuid nullable FK → students | null if caller not recognised |
| phone_from | text | |
| intent_detected | text | derived from tool call set |
| outcome | text | derived from tool call set |
| escalated | boolean | true if `notify_secretary` was called |
| duration_seconds | int | |
| created_at | timestamptz | |

### `trial_sessions`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| student_id | uuid FK → students | |
| course_id | uuid FK → courses | |
| date | date | |
| created_at | timestamptz | |

Unique constraint on (student_id, course_id) — one free session per course per student.

### `settings`
| Column | Type | Notes |
|--------|------|-------|
| key | text PK | |
| value | text | |
| updated_at | timestamptz | |

Currently used: `trial_week_active` (`"true"` / `"false"`).

---

## How a Call Works (Step by Step)

```
1. Caller dials +1 XXX XXX XXXX (Twilio number)
2. Twilio POSTs to /incoming-call
   └── Server validates Twilio signature (HMAC)
   └── Server mints a short-lived HMAC token (30s TTL)
   └── Returns TwiML: <Stream url="wss://…/media-stream">
                        <Parameter name="from" value="+39…" />
                        <Parameter name="token" value="…" />
3. Twilio opens WebSocket to /media-stream
4. Server accepts WebSocket, starts 3 async tasks:
   ├── deepgram_sender: feeds audio → Deepgram
   ├── llm_worker: awaits transcripts from llm_queue
   └── tts_sender: awaits sentences from tts_queue
5. Twilio sends "start" event with customParameters
   └── Server verifies token (closes WebSocket on failure)
   └── Server looks up caller phone → Supabase students
       ├── If found: injects student context as system message
       └── Checks trial_week_active → injects trial context if needed
   └── Enqueues greeting to tts_queue → ElevenLabs → Twilio audio out
6. Twilio streams mulaw audio frames → audio_queue → Deepgram
   ├── Deepgram interim transcript (non-empty): if is_speaking → barge-in
   │   └── drains tts_queue, sends {"event":"clear"} to Twilio
   └── Deepgram "is_final" transcript → llm_queue
7. llm_worker dequeues transcript
   └── Builds messages: [system_prompt] + [student context] + [last 20 turns]
   └── Calls GPT-4o with tool definitions (streaming)
   ├── If tool_calls in response:
   │   ├── Dispatches all tool calls concurrently (asyncio.gather)
   │   ├── Appends tool results to messages
   │   └── Loops back to GPT-4o (up to 10 iterations)
   └── If text response:
       └── Splits on sentence boundaries → streams sentences to tts_queue
8. tts_sender dequeues sentences
   └── Streams the sentence over the persistent ElevenLabs WebSocket (one context per sentence, auto_mode, ulaw_8000)
   └── Sends 160-byte mulaw frames to Twilio media
9. Twilio sends "stop" event (caller hangs up)
10. Server tears down: cancels Deepgram, drains queues, awaits tasks
    └── Inserts call_log (intent + outcome derived from tools_called set)
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token (also used to validate inbound signatures) |
| `TWILIO_PHONE_NUMBER` | Inbound phone number in E.164 |
| `TWILIO_WHATSAPP_FROM` | WhatsApp-enabled Twilio number for secretary notifications |
| `SECRETARY_WHATSAPP` | Secretary's WhatsApp number (E.164) |
| `DEEPGRAM_API_KEY` | Deepgram API key (Nova-2 streaming) |
| `OPENAI_API_KEY` | OpenAI API key (GPT-4o) |
| `ELEVENLABS_API_KEY` | ElevenLabs API key (TTS, eleven_flash_v2_5) |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice ID to use for TTS |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (server-side only, never exposed to clients) |
| `WS_TOKEN_SECRET` | Secret for HMAC WebSocket tokens; must be identical across all instances |

---

## Local Setup

**Prerequisites**: Python 3.11+

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS

pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in all values. Then:

```bash
uvicorn main:app --reload --port 8000
```

To test locally with Twilio, expose port 8000 via ngrok and point the Twilio number's voice webhook to `https://<ngrok-id>.ngrok.io/incoming-call`.

---

## Production Deployment (Render)

The app is deployed as a Render Web Service. Key configuration:

- **Start command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- **Health check path**: `/health`
- **Auto-deploy**: enabled (push to `main` → Render builds and deploys)

Render provides automatic TLS, which is required by Twilio for both the webhook (`/incoming-call`) and the WebSocket (`/media-stream`). The `x-forwarded-proto` header is used to reconstruct the correct public URL for Twilio signature validation.

The Supabase keepalive loop (pings the DB every 5 days) prevents the connection from being dropped by Supabase's 7-day idle policy on free plans.

---

## Admin Dashboard

A separate Next.js (App Router) application provides the monitoring and analytics surface for the
school's staff. It is a distinct, access-controlled deployment — **not publicly accessible**, so it
is documented here rather than linked for anonymous use.

**Stack**: Next.js (App Router, Server Components) · `@supabase/ssr` for cookie-based auth ·
Tailwind v4 · shadcn/ui · Recharts · deployed on Vercel. Auth is enforced server-side (a Server
Action signs in; the dashboard layout redirects to `/login` without a session). It reads the same
Supabase project as the voice agent, through the **anon key under RLS** (authenticated-only SELECT
policies) — never the service role key.

**What it shows**:
- **KPIs**: calls today · average response latency (with average TTFT) · contain rate (share of
  calls handled without human escalation) · completion rate (share reaching a useful outcome —
  booking, recovery, info) · average call duration · bookings created.
- **28-day call volume** bar chart.
- **Recent calls table**: timestamp, caller, detected intent, outcome, duration, per-call latency,
  escalation flag.

The latency, contain-rate and completion-rate metrics map directly to the standard voice-agent
KPIs (end-to-end latency, containment, completion). Latency is sourced from the `avg_response_ms` /
`avg_ttft_ms` / `n_turns` columns the agent writes to `call_logs` at the end of each call.

---

## Evals & Observability

The agent measures itself, end to end.

**Telemetry.** Every call writes per-turn rows to `turn_metrics` (TTFT, full-LLM, TTS-TTFB,
end-to-end response latency, tool rounds/time, token usage, synthesized characters) and a per-call
rollup to `call_traces` (token totals, synthesized chars, barge-in count, and an approximate
`cost_usd` combining GPT-4o tokens, ElevenLabs characters and Twilio minutes). The `/observability`
dashboard surfaces per-stage latency, end-to-end p50/p95, cost per call and barge-ins. (STT-stage
latency is intentionally not isolated — on a phone line it is dominated by Deepgram endpointing,
which happens outside the process.)

**Reproducible eval suite** (`evals/`). `run_evals.py` runs a fixed set of scenarios (course info,
instructor lookup, pricing, multi-turn booking, trial check, Spanish) through the **same system
prompt, tool schema, tool functions and model** the production agent uses, and scores
task-success = the expected tool was called. Reads hit real Supabase; write tools are intercepted
(not persisted) so runs are reproducible. Each run writes `eval_runs` + `eval_results`, so the
`/evals` dashboard shows success rate, latency p50/p95, and the trend across runs.

```
$ python -m evals.run_evals
6/6 passed (100%) — p50 4198ms · p95 6289ms
```

This is what turns "I built a voice agent" into "I built a voice agent I can measure and regression-test".

---

## Stack

| Component | Library | Version |
|-----------|---------|---------|
| Runtime | Python | 3.11+ |
| Web framework | FastAPI | 0.115.5 |
| ASGI server | Uvicorn | 0.32.1 |
| Telephony | twilio | 9.3.6 |
| STT | deepgram-sdk | 3.7.7 |
| LLM | openai (GPT-4o) | 1.57.0 |
| TTS | elevenlabs (eleven_flash_v2_5) | 1.54.0 |
| Database | supabase | 2.10.0 |

**Python version note.** `.python-version` pins 3.11.9 for local development, but the app runs
on any Python ≥3.11 with no version-specific dependencies. (The former caveat — `audioop`,
removed from the stdlib in 3.13 — disappeared when TTS switched to Twilio-native `ulaw_8000`
output: there is no resample step anymore, so the `audioop-lts` backport was dropped.)

---

## Production Considerations

**What's production-ready**
- Full call lifecycle with barge-in, concurrent task architecture, and graceful teardown
- Server-side business rule enforcement (recovery levels, capacity) independent of LLM behaviour
- HMAC WebSocket auth prevents unauthenticated connections to the media endpoint
- Twilio signature validation on every inbound webhook
- DB-level capacity constraints as a safety net behind the application-level checks
- Structured call logging derived from tool calls (not LLM text)

**What would need work before a real client**
- LLM history is per-connection, in-memory — no persistence across calls, no call continuity if the process restarts mid-call
- The Supabase keepalive assumes a single process; a multi-worker deployment should use a dedicated scheduler
- Error recovery is basic: a failed tool call logs the error and returns a user-facing message, but there's no retry or circuit-breaker logic
- No admin UI — course schedule and settings are managed directly in Supabase
- No test suite; the pipeline is exercised manually via live calls
