# dance-voice-agent

Inbound voice agent for a Latin dance school. Handles student calls autonomously — course information, lesson bookings, recovery bookings, and secretary escalation via WhatsApp. Deployed in production on Render with Twilio telephony.

The agent speaks Italian, identifies callers by phone number, and operates within strict business rules (recovery level constraints, capacity checks). All conversation state is ephemeral; persistent state lives in Supabase.

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
    └──► ElevenLabs TTS (eleven_v3)
             │  PCM 24kHz streamed chunks
             │  audioop: resample 24kHz→8kHz, lin2ulaw
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

**Audio conversion in stdlib**
ElevenLabs TTS outputs PCM s16le at 24kHz. Twilio expects mulaw at 8kHz. The conversion uses `audioop.ratecv` (resample, preserving state across streaming chunks) and `audioop.lin2ulaw` — both in Python's standard library, no additional dependencies. Output is buffered and flushed in exact 160-byte frames to maintain mulaw frame alignment on the Twilio side.

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
   └── Calls ElevenLabs TTS (PCM 24kHz stream, in thread executor)
   └── audioop: resample 24kHz→8kHz, lin2ulaw
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
| `ELEVENLABS_API_KEY` | ElevenLabs API key (TTS, eleven_v3) |
| `ELEVENLABS_VOICE_ID` | ElevenLabs voice ID to use for TTS |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key (server-side only, never exposed to clients) |
| `WS_TOKEN_SECRET` | Secret for HMAC WebSocket tokens; must be identical across all instances |

---

## Local Setup

**Prerequisites**: Python 3.11+ (see the note on `audioop` under Stack for the 3.13+ caveat)

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

## Stack

| Component | Library | Version |
|-----------|---------|---------|
| Runtime | Python | 3.11+ (3.13+ needs `audioop-lts`) |
| Web framework | FastAPI | 0.115.5 |
| ASGI server | Uvicorn | 0.32.1 |
| Telephony | twilio | 9.3.6 |
| STT | deepgram-sdk | 3.7.7 |
| LLM | openai (GPT-4o) | 1.57.0 |
| TTS | elevenlabs (eleven_v3) | 1.54.0 |
| Database | supabase | 2.10.0 |

**Python version note.** `.python-version` pins 3.11.9 for local development, but the app runs
on any Python ≥3.11. The one caveat is `audioop` (used for the TTS resample/μ-law conversion in
`main.py`): it was removed from the standard library in Python 3.13 (PEP 594). On 3.13+ the
`audioop-lts` backport — declared in `requirements.txt` with an environment marker — restores it,
so the build works on newer runtimes (e.g. Render's current default image) with no code change.

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
