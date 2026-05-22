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
    │        ├── SpeechStarted event ──► barge-in: clear Twilio buffer + interrupt TTS
    │        └── is_final transcript ──► LLM queue
    │
    ├──► GPT-4o-mini (agentic loop)
    │        │  tool calls
    │        ├── get_student_by_phone ──► Supabase
    │        ├── get_courses          ──► Supabase
    │        ├── create_booking       ──► Supabase
    │        ├── create_recovery      ──► Supabase
    │        └── notify_secretary     ──► Twilio WhatsApp API
    │
    └──► OpenAI TTS (tts-1, nova)
             │  PCM 24kHz stream
             │  audioop: resample 24kHz→8kHz, lin2ulaw
             │  160-byte frames (20ms mulaw)
             ▼
         Twilio (audio out)
```

Three concurrent async tasks per call — `deepgram_sender`, `llm_worker`, `tts_sender` — communicate via asyncio queues. The main WebSocket loop feeds raw audio to Deepgram and dispatches Twilio stream events.

---

## Key Technical Decisions

**Custom pipeline over managed platforms**
Platforms like Vapi, Bland, or Twilio AI Assistants abstract the audio pipeline in exchange for vendor lock-in, limited control over STT/LLM/TTS choice, and higher per-minute costs. The custom pipeline runs each component independently, making it straightforward to swap any layer (STT, LLM, TTS) without touching the others. The WebSocket handler is ~400 lines and owns the full call lifecycle.

**Deepgram for STT**
Deepgram Nova-2 is the only STT provider with a native streaming WebSocket API designed for telephony (mulaw, 8kHz, 8-bit). The `SpeechStarted` VAD event — fired before any transcript is produced — is the mechanism used for barge-in. Alternatives (Whisper, Google Speech) are batch or introduce higher latency; partial transcripts alone would add 300–500ms to barge-in response time.

**Barge-in via SpeechStarted**
When `SpeechStarted` fires while the agent is speaking (`is_speaking = True`): the TTS stream is abandoned mid-chunk, the `tts_queue` is drained, and a `{"event": "clear"}` message is sent to Twilio to flush the audio buffer on the caller's end. Partial transcripts act as a fallback. The `is_speaking` flag is checked at every 160-byte frame boundary in `tts_sender` so interruption is near-immediate.

**Audio conversion in stdlib**
OpenAI TTS outputs PCM s16le at 24kHz. Twilio expects mulaw at 8kHz. The conversion uses `audioop.ratecv` (resample, preserving state across streaming chunks) and `audioop.lin2ulaw` — both in Python's standard library, no additional dependencies. Output is buffered and flushed in exact 160-byte frames to maintain mulaw frame alignment on the Twilio side.

---

## Agent Capabilities

**`get_student_by_phone`**
Lookup by caller's phone number (injected from the Twilio stream `start` event). If found, the student's name, level, and subscription status are injected into the LLM context before the first user turn. Unrecognised numbers are handled conversationally.

**`get_courses`**
Queries the `courses` table filtered by `active = true`, with optional `level` and `location` parameters. The LLM is instructed to always call this tool before answering any course-related question — it has no fallback knowledge of the schedule.

**`create_booking`**
Inserts a `regular` booking after verifying capacity: counts confirmed bookings for the requested course/date and rejects if `confirmed >= max_capacity`. The LLM must confirm the details aloud before calling this tool.

**`create_recovery`**
Enforces the school's recovery rules in code:
```python
RECOVERY_RULES = {
    "intermedio": ["base"],
    "avanzato":   ["intermedio", "base"],
    "base":       [],
}
```
A student can only attend a recovery class at a strictly lower level than their own. The check runs server-side regardless of what the LLM requests. Capacity is also verified before inserting.

**`notify_secretary`**
Sends a WhatsApp message via Twilio to the school's secretary number when the agent cannot resolve a request autonomously (complaints, payments, out-of-scope queries). The message includes the caller's phone number and a description of the problem. Uses the synchronous Twilio client wrapped in `asyncio.to_thread`.

**Call logging**
At WebSocket close, a record is inserted into `call_logs` with `intent_detected` and `outcome` derived from the set of tool calls made during the session — not from LLM text. Priority order: `escalation > prenotazione > recupero > info_corsi > unknown`.

---

## Stack

| Component | Library | Version |
|-----------|---------|---------|
| Runtime | Python | 3.11.9 |
| Web framework | FastAPI | 0.115.5 |
| ASGI server | Uvicorn | 0.32.1 |
| Telephony | twilio | 9.3.6 |
| STT | deepgram-sdk | 3.7.7 |
| LLM | openai | 1.57.0 |
| TTS | openai (tts-1) | 1.57.0 |
| Database | supabase | 2.10.0 |
| HTTP client | httpx | transitive |

Python 3.11.9 is pinned because the Deepgram SDK is incompatible with Python 3.13+.

---

## Infrastructure

**Render**
FastAPI app deployed as a web service. Twilio requires a stable HTTPS/WSS endpoint; Render provides this with automatic TLS. The WebSocket path `/media-stream` must remain accessible without timeout — Render's default idle timeout is disabled for WebSocket connections.

**Supabase**
PostgreSQL (eu-west-1). Four tables: `students`, `courses`, `bookings`, `call_logs`. The service role key is used server-side only; RLS is currently disabled (internal tool, no public client access).

**Twilio**
Inbound phone number configured to POST to `/incoming-call` on call start. The TwiML response opens a `<Stream>` WebSocket to `/media-stream` and passes the caller's number as a custom parameter. WhatsApp sender is a separate Twilio number used exclusively for secretary notifications.

**Deployment flow**
Push to `main` → Render auto-deploys. No CI pipeline. Environment variables (Twilio, Deepgram, OpenAI, Supabase, Cartesia keys) are set in Render's environment config and loaded via `python-dotenv` locally.
