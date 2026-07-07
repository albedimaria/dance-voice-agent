from dotenv import load_dotenv

load_dotenv()

import os

import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import re
import secrets
import time
import traceback

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from supabase import create_client, Client
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from openai import AsyncOpenAI

from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from websockets.asyncio.client import connect as ws_connect
from prompt import SYSTEM_PROMPT
from pricing import call_cost_usd
from tools_schema import OPENAI_TOOLS
from tools.supabase_tools import get_student_by_phone, get_courses, create_booking, create_recovery, notify_secretary, get_settings, check_trial_used, create_trial_session, get_pricing

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)


async def _keepalive_loop() -> None:
    INTERVAL = 5 * 24 * 3600  # 5 giorni, sotto la soglia di 7 di Supabase
    while True:
        await asyncio.sleep(INTERVAL)
        try:
            supabase.table("settings").select("key").limit(1).execute()
            print("[keepalive] Supabase ping ok")
        except Exception as exc:
            print(f"[keepalive] errore: {exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(_keepalive_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="dance-voice-agent", lifespan=lifespan)

twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
tw_validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])

deepgram = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])

openai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])

# Stateless HMAC tokens for WebSocket auth — works with multiple workers.
# WS_TOKEN_SECRET must be the same across all instances (set it in env).
_WS_SECRET: str = os.environ.get("WS_TOKEN_SECRET", "")


def _make_ws_token() -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    sig = hmac.new(_WS_SECRET.encode(), f"{ts}:{nonce}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{nonce}.{sig}"


def _verify_ws_token(token: str, ttl: int = 30) -> bool:
    try:
        ts_s, nonce, sig = token.split(".", 2)
        if abs(int(time.time()) - int(ts_s)) > ttl:
            return False
        expected = hmac.new(
            _WS_SECRET.encode(), f"{ts_s}:{nonce}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


# OPENAI_TOOLS is imported from tools_schema (shared with the eval runner).


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/incoming-call")
async def incoming_call(request: Request) -> Response:
    form = dict(await request.form())
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    url = str(request.url).replace(f"{request.url.scheme}://", f"{proto}://", 1)
    signature = request.headers.get("X-Twilio-Signature", "")
    if not tw_validator.validate(url, form, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    host = request.headers.get("host", request.base_url.hostname)
    token = _make_ws_token()
    stream_url = f"wss://{host}/media-stream"  # Twilio ignora query params sugli Stream

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=stream_url)
    stream.parameter(name="from", value=form.get("From", ""))
    stream.parameter(name="token", value=token)  # arriva in start.customParameters
    connect.append(stream)
    response.append(connect)

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    print("[stream] WebSocket accettato — in attesa di start con token")

    stream_sid: str = ""
    caller_phone: str = ""
    tts_language: str = "it"  # updated from student.language_preference on call start
    dg_connection = deepgram.listen.asynclive.v("1")
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    llm_queue: asyncio.Queue[dict | None] = asyncio.Queue()
    tts_queue: asyncio.Queue[dict | None] = asyncio.Queue()
    history: list[dict] = []
    is_speaking: bool = False
    llm_busy: bool = False
    call_start: float = time.time()
    student_id: str | None = None
    tools_called: set[str] = set()
    latency_response_ms: list[float] = []  # per-turn STT-final -> first frame out
    latency_ttft_ms: list[float] = []      # per-turn STT-final -> first LLM token
    all_turn_timings: list[dict] = []      # full per-turn timing dicts → turn_metrics
    barge_in_count: int = 0
    last_barge_in_time: float = 0.0
    BARGE_IN_COOLDOWN: float = 0.8
    call_sid: str = ""
    last_activity: float = time.time()
    hangup_requested: bool = False
    INACTIVITY_TIMEOUT: float = 12.0  # silence after the agent is done → auto hang up

    async def _barge_in() -> None:
        nonlocal is_speaking, last_barge_in_time, barge_in_count
        if not is_speaking:
            return
        is_speaking = False
        barge_in_count += 1
        last_barge_in_time = time.time()
        while not tts_queue.empty():
            tts_queue.get_nowait()
        await websocket.send_text(json.dumps({
            "event": "clear",
            "streamSid": stream_sid,
        }))
        print("[barge-in] TTS interrotto")

    async def _hangup(reason: str) -> None:
        # End the phone call from the agent side (so the caller doesn't have to).
        # Ending the Twilio call is what actually hangs up the PSTN leg; closing
        # only the WebSocket would leave the call open and silent.
        nonlocal hangup_requested
        if hangup_requested:
            return
        hangup_requested = True
        await asyncio.sleep(0.8)  # let a closing line start generating
        for _ in range(150):      # wait for the agent to finish speaking (~15s cap)
            if not is_speaking and tts_queue.empty():
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.4)  # grace so the last audio frame isn't clipped
        print(f"[hangup] chiusura chiamata dall'agente ({reason})")
        if call_sid:
            try:
                await asyncio.to_thread(
                    lambda: twilio.calls(call_sid).update(status="completed")
                )
            except Exception as exc:
                print(f"[hangup] errore Twilio: {exc}")
        try:
            await websocket.close()
        except Exception:
            pass

    async def inactivity_watchdog() -> None:
        # Fix for "caller goes silent and the call stays open forever": once the
        # agent is idle and there's been no user input for INACTIVITY_TIMEOUT,
        # say a short goodbye and hang up.
        while not hangup_requested:
            await asyncio.sleep(1.0)
            idle = time.time() - last_activity
            if (idle > INACTIVITY_TIMEOUT and not is_speaking
                    and not llm_busy and tts_queue.empty()):
                await tts_queue.put({
                    "text": "Va bene, se non ti serve altro ti saluto. A presto!",
                    "timing": None,
                })
                await _hangup("inattività")
                return

    async def on_speech_started(*args, **kwargs) -> None:
        print("[VAD] parlato rilevato")

    async def on_transcript(*args, **kwargs) -> None:
        nonlocal last_activity
        try:
            result = kwargs.get("result")
            if result is None:
                return
            transcript = result.channel.alternatives[0].transcript
            if not transcript:
                return
            if time.time() - last_barge_in_time < BARGE_IN_COOLDOWN:
                print(f"[STT cooldown] ignorato: {transcript}")
                return
            if result.is_final:
                last_activity = time.time()
                print(f"[STT FINAL] {transcript}")
                while not llm_queue.empty():
                    try:
                        llm_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await llm_queue.put({"text": transcript, "t0": time.perf_counter()})
            else:
                print(f"[STT partial] {transcript}")
                await _barge_in()
        except Exception:
            print(f"[STT] errore on_transcript:\n{traceback.format_exc()}")

    dg_connection.on(LiveTranscriptionEvents.SpeechStarted, on_speech_started)
    dg_connection.on(LiveTranscriptionEvents.Transcript, on_transcript)

    options = LiveOptions(
        model="nova-2",
        language="it",
        encoding="mulaw",
        sample_rate=8000,
        channels=1,
        interim_results=True,
        utterance_end_ms="1000",
        vad_events=True,
    )

    async def deepgram_sender() -> None:
        try:
            started = await dg_connection.start(options)
            print(f"[deepgram] connessione avviata: {started}")
            while True:
                audio = await audio_queue.get()
                if audio is None:
                    break
                await dg_connection.send(audio)
        except Exception as exc:
            print(f"[deepgram] errore: {exc}")
        finally:
            await dg_connection.finish()

    async def llm_worker() -> None:
        nonlocal llm_busy

        sentence_re = re.compile(r'(?<=[.!?])\s+')

        # Per-turn latency timing, threaded to the TTS worker via the first sentence.
        turn_timing: dict | None = None
        first_emitted = False

        async def emit_tts(sentence: str) -> None:
            # Attach the turn's timing dict to the FIRST sentence only; the TTS
            # worker stamps t2/t3 and logs the breakdown when it plays it.
            nonlocal first_emitted
            if turn_timing is not None:
                turn_timing["tts_chars"] += len(sentence)  # all sentences of the turn
            if not first_emitted:
                first_emitted = True
                if turn_timing is not None:
                    turn_timing["t_first_sentence"] = time.perf_counter()
                await tts_queue.put({"text": sentence, "timing": turn_timing})
            else:
                await tts_queue.put({"text": sentence, "timing": None})

        async def _dispatch_tool(tc_id: str, fn: str, raw_args: str) -> tuple[str, dict]:
            try:
                args = json.loads(raw_args)
            except Exception:
                return tc_id, {"error": "argomenti JSON non validi"}
            print(f"[LLM] tool call: {fn}({args})")
            tools_called.add(fn)
            if fn == "get_courses":
                result = await get_courses(supabase, **args)
            elif fn == "create_booking":
                result = await create_booking(supabase, **args)
            elif fn == "create_recovery":
                result = await create_recovery(supabase, **args)
            elif fn == "notify_secretary":
                result = await notify_secretary(caller_phone=caller_phone, twilio_client=twilio, **args)
            elif fn == "get_settings":
                result = await get_settings(supabase)
            elif fn == "check_trial_used":
                result = await check_trial_used(supabase, **args)
            elif fn == "create_trial_session":
                result = await create_trial_session(supabase, **args)
            elif fn == "get_pricing":
                result = get_pricing(**args)
            elif fn == "end_call":
                # Agent decided the conversation is over; hang up after the
                # closing line it produces in this same turn.
                asyncio.create_task(_hangup("fine conversazione"))
                result = {"ended": True}
            else:
                result = {"error": f"tool {fn!r} non implementato"}
            print(f"[LLM] tool result ({fn}): {result}")
            return tc_id, result

        while True:
            item = await llm_queue.get()
            if item is None:
                break
            text = item["text"]
            turn_timing = {
                "t0": item["t0"],          # STT final transcript received
                "t_first_token": None,     # LLM first content token
                "t_llm_end": None,         # LLM last token (full response)
                "t_first_sentence": None,  # first sentence handed to TTS
                "t_tts_end": None,         # first sentence last TTS chunk
                "tool_ms": 0.0,            # cumulative tool execution time
                "tool_rounds": 0,
                "prompt_tokens": 0,        # summed across LLM rounds this turn
                "completion_tokens": 0,
                "tts_chars": 0,            # summed across sentences this turn
            }
            all_turn_timings.append(turn_timing)
            first_emitted = False
            llm_busy = True
            print(f"[LLM] input: {text}")
            history.append({"role": "user", "content": text})
            try:
                # system messages (student identity, trial context) are always kept;
                # only user/assistant turns are trimmed to the last 20
                system_msgs = [m for m in history if m["role"] == "system"]
                turn_msgs = [m for m in history if m["role"] != "system"]
                messages = [{"role": "system", "content": SYSTEM_PROMPT}] + system_msgs + turn_msgs[-20:]
                for _ in range(10):  # else-branch fires if loop exhausts without break
                    tool_calls_acc: dict[int, dict] = {}
                    text_acc = ""
                    sentence_buf = ""

                    stream = await asyncio.wait_for(
                        openai.chat.completions.create(
                            model="gpt-4o",
                            messages=messages,
                            tools=OPENAI_TOOLS,
                            tool_choice="auto",
                            stream=True,
                            stream_options={"include_usage": True},
                        ),
                        timeout=10.0,
                    )

                    async for chunk in stream:
                        # The final chunk (include_usage) carries token usage and no choices.
                        if getattr(chunk, "usage", None) and turn_timing is not None:
                            turn_timing["prompt_tokens"] += chunk.usage.prompt_tokens or 0
                            turn_timing["completion_tokens"] += chunk.usage.completion_tokens or 0
                        if not chunk.choices:
                            continue
                        delta = chunk.choices[0].delta

                        if delta.tool_calls:
                            for tc in delta.tool_calls:
                                idx = tc.index
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {"id": "", "name": "", "arguments": ""}
                                if tc.id:
                                    tool_calls_acc[idx]["id"] = tc.id
                                if tc.function:
                                    if tc.function.name:
                                        tool_calls_acc[idx]["name"] += tc.function.name
                                    if tc.function.arguments:
                                        tool_calls_acc[idx]["arguments"] += tc.function.arguments

                        elif delta.content:
                            if turn_timing is not None and turn_timing["t_first_token"] is None:
                                turn_timing["t_first_token"] = time.perf_counter()
                            text_acc += delta.content
                            sentence_buf += delta.content
                            while True:
                                m = sentence_re.search(sentence_buf)
                                if not m:
                                    break
                                sentence = sentence_buf[:m.start() + 1].strip()
                                sentence_buf = sentence_buf[m.end():]
                                if sentence:
                                    await emit_tts(sentence)

                    if tool_calls_acc:
                        messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                                }
                                for tc in tool_calls_acc.values()
                            ],
                        })
                        _tool_start = time.perf_counter()
                        results = await asyncio.gather(*[
                            _dispatch_tool(tc["id"], tc["name"], tc["arguments"])
                            for tc in tool_calls_acc.values()
                        ])
                        if turn_timing is not None:
                            turn_timing["tool_ms"] += (time.perf_counter() - _tool_start) * 1000
                            turn_timing["tool_rounds"] += 1
                        for tc_id, result in results:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                    else:
                        if turn_timing is not None:
                            turn_timing["t_llm_end"] = time.perf_counter()
                        if sentence_buf.strip():
                            await emit_tts(sentence_buf.strip())
                        history.append({"role": "assistant", "content": text_acc})
                        print(f"[LLM] risposta: {text_acc}")
                        break
                else:
                    fallback = "Mi dispiace, ho avuto un problema tecnico. Riprova o contatta la segreteria."
                    await emit_tts(fallback)
                    history.append({"role": "assistant", "content": fallback})
                    print("[LLM] limite iterazioni raggiunto — fallback inviato")

            except Exception:
                print(f"[LLM] errore:\n{traceback.format_exc()}")
                history.pop()
            finally:
                llm_busy = False

    async def tts_sender() -> None:
        nonlocal is_speaking, last_activity
        FRAME = 160  # 20ms @ mulaw 8kHz
        current_timing: dict | None = None  # timing of the turn currently playing

        def _log_latency(ct: dict) -> None:
            def ms(a, b) -> str:
                return f"{(b - a) * 1000:.0f}ms" if a and b else "n/a"
            # Accumulate per-call samples for the persisted aggregate (see _insert_log).
            t0, t3, tft = ct["t0"], ct.get("t3"), ct.get("t_first_token")
            if t3:
                latency_response_ms.append((t3 - t0) * 1000)
            if tft:
                latency_ttft_ms.append((tft - t0) * 1000)
            # ttft (t0→first content token) includes tool time on tool turns;
            # tool_ms is reported separately so it can be reasoned about.
            print(
                f"[latency] ttft={ms(ct['t0'], tft)} "
                f"tts_ttfb={ms(ct.get('t_first_sentence'), ct.get('t2'))} "
                f"total_response={ms(ct['t0'], t3)} "
                f"tool_rounds={ct.get('tool_rounds', 0)} "
                f"tool_ms={ct.get('tool_ms', 0):.0f}"
            )

        async def _send_frame(data: bytes) -> None:
            # First audio frame of a timed turn → stamp t3 and log the breakdown.
            if current_timing is not None and not current_timing.get("_logged"):
                current_timing["_logged"] = True
                current_timing["t3"] = time.perf_counter()
                _log_latency(current_timing)
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(data).decode()},
            }))

        # Persistent multi-context TTS WebSocket: one connection per call, kept
        # warm across sentences and turns (ulaw_8000 is Twilio's native format:
        # no resample step, no audioop dependency). auto_mode lets ElevenLabs
        # start generating as soon as a context closes, with no manual chunk
        # scheduling. Each sentence gets its own context so a barge-in abandons
        # one context while later sentences start clean; frames still in flight
        # for an abandoned context are filtered out by context id. ElevenLabs
        # drops idle sockets (~20s), so connection is re-established lazily.
        eleven_ws = None
        eleven_ws_lang: str | None = None
        ctx_seq = 0

        def _ws_url() -> str:
            return (
                f"wss://api.elevenlabs.io/v1/text-to-speech/"
                f"{os.environ['ELEVENLABS_VOICE_ID']}/multi-stream-input"
                f"?model_id=eleven_flash_v2_5&output_format=ulaw_8000"
                f"&auto_mode=true&language_code={tts_language}"
            )

        async def _ws_open() -> None:
            nonlocal eleven_ws, eleven_ws_lang
            if eleven_ws is not None:
                try:
                    await eleven_ws.close()
                except Exception:
                    pass
            eleven_ws = await ws_connect(
                _ws_url(),
                additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                open_timeout=10.0,
            )
            eleven_ws_lang = tts_language

        # Warm up the TLS+WS handshake before the first sentence needs it.
        try:
            await _ws_open()
        except Exception as exc:
            print(f"[TTS] pre-connect fallito (riproverò alla prima frase): {exc}")
            eleven_ws = None

        while True:
            item = await tts_queue.get()
            if item is None:
                break
            text = item["text"]
            current_timing = item.get("timing")
            print(f"[TTS] sintetizzando: {text} (lang={tts_language})")
            try:
                ctx_seq += 1
                ctx = f"s{ctx_seq}"
                # Whole sentence + close_context: with auto_mode, closing the
                # context triggers generation immediately. One retry on a fresh
                # connection covers idle-timeout drops and language switches.
                for attempt in (0, 1):
                    try:
                        if eleven_ws is None or eleven_ws_lang != tts_language:
                            await _ws_open()
                        await eleven_ws.send(json.dumps({"text": text + " ", "context_id": ctx}))
                        await eleven_ws.send(json.dumps({"context_id": ctx, "close_context": True}))
                        break
                    except Exception:
                        if attempt == 1:
                            raise
                        eleven_ws = None

                buf = b""
                is_speaking = True
                while True:
                    raw = await asyncio.wait_for(eleven_ws.recv(), timeout=15.0)
                    msg = json.loads(raw)
                    cid = msg.get("contextId") or msg.get("context_id")
                    if cid is not None and cid != ctx:
                        continue  # in-flight frame of an abandoned context
                    chunk = base64.b64decode(msg["audio"]) if msg.get("audio") else b""
                    if chunk:
                        if not is_speaking:
                            break
                        if current_timing is not None and current_timing.get("t2") is None:
                            current_timing["t2"] = time.perf_counter()
                        buf += chunk
                        while len(buf) >= FRAME:
                            if not is_speaking:
                                break
                            await _send_frame(buf[:FRAME])
                            buf = buf[FRAME:]
                    if msg.get("isFinal") or msg.get("is_final"):
                        # End of this sentence's synthesis; stamp TTS end for the timed turn.
                        if current_timing is not None and current_timing.get("t_tts_end") is None:
                            current_timing["t_tts_end"] = time.perf_counter()
                        break
                if is_speaking and buf:
                    await _send_frame(buf)

            except Exception as exc:
                print(f"[TTS] errore: {exc}")
                # A failed send/read leaves the socket in an unknown state;
                # drop it so the next sentence starts from a clean connection.
                try:
                    if eleven_ws is not None:
                        await eleven_ws.close()
                except Exception:
                    pass
                eleven_ws = None
            finally:
                is_speaking = False
                last_activity = time.time()  # agent finished talking → start silence clock

        if eleven_ws is not None:
            try:
                await eleven_ws.send(json.dumps({"close_socket": True}))
                await eleven_ws.close()
            except Exception:
                pass

    dg_task = asyncio.create_task(deepgram_sender())
    llm_task = asyncio.create_task(llm_worker())
    tts_task = asyncio.create_task(tts_sender())
    watchdog_task = asyncio.create_task(inactivity_watchdog())

    try:
        while True:
            if dg_task.done():
                print("[stream] deepgram terminato inaspettatamente — chiudo chiamata")
                break
            message = await websocket.receive_text()
            data = json.loads(message)
            event = data.get("event")

            if event == "media":
                audio = base64.b64decode(data["media"]["payload"])
                await audio_queue.put(audio)
            elif event == "connected":
                print("[twilio] connected")
            elif event == "start":
                stream_sid = data["start"]["streamSid"]
                call_sid = data["start"].get("callSid") or ""
                custom_params = data["start"].get("customParameters", {})
                caller_phone = custom_params.get("from", "")
                ws_token = custom_params.get("token", "")
                if not _verify_ws_token(ws_token):
                    print("[stream] token non valido nel start message — chiudo")
                    await websocket.close(code=1008)
                    break
                print(f"[stream] avviato — callSid={data['start'].get('callSid')} from={caller_phone}")
                if caller_phone:
                    student = await get_student_by_phone(supabase, caller_phone)
                    if student:
                        student_id = student["id"]
                        tts_language = student.get("language_preference", "it")
                        print(f"[DB] studente: {student['first_name']} {student['last_name']} ({student['level']}) lang={tts_language}")
                        history.append({
                            "role": "system",
                            "content": (
                                f"Stai parlando con {student['first_name']} {student['last_name']}, "
                                f"livello {student['level']}. "
                                f"Abbonamento attivo: {'sì' if student['active_subscription'] else 'no'}. "
                                f"student_id: {student['id']}."
                            ),
                        })
                        if tts_language == "es":
                            history.append({
                                "role": "system",
                                "content": "Este estudiante prefiere hablar en español. Responde siempre en español.",
                            })
                    else:
                        print(f"[DB] numero non trovato: {caller_phone}")
                settings = await get_settings(supabase)
                if settings.get("trial_week_active") == "true":
                    history.append({
                        "role": "system",
                        "content": "CONTESTO: Settimana di prova attiva. Tutti i corsi sono gratuiti e aperti.",
                    })
                    print("[settings] settimana di prova attiva")
                # AI-disclosure at first contact (EU AI Act Art. 50(1)): the caller
                # is told up front they're talking to an automated system.
                greeting = "Ciao! Sono TropicoCHETA, l'assistente vocale automatico di Ritmo Caliente. Come posso aiutarti?"
                await tts_queue.put({"text": greeting, "timing": None})
                history.append({"role": "assistant", "content": greeting})
            elif event == "stop":
                print("[stream] terminato")
                break
            else:
                print(f"[twilio] evento sconosciuto: {event}")
    except Exception as exc:
        print(f"[stream] errore ricezione: {exc}")
    finally:
        watchdog_task.cancel()
        await audio_queue.put(None)
        await llm_queue.put(None)
        await tts_queue.put(None)
        await dg_task
        await llm_task
        await tts_task

        def _derive_intent() -> str:
            if "notify_secretary" in tools_called:
                return "escalation"
            if "create_booking" in tools_called:
                return "prenotazione"
            if "create_recovery" in tools_called:
                return "recupero"
            if "get_courses" in tools_called:
                return "info_corsi"
            return "unknown"

        def _derive_outcome() -> str:
            if "notify_secretary" in tools_called:
                return "escalato alla segreteria"
            if "create_booking" in tools_called:
                return "prenotazione_confermata"
            if "create_recovery" in tools_called:
                return "recupero_confermato"
            if "get_courses" in tools_called:
                return "info_fornite"
            return "unknown"

        def _avg(samples: list[float]) -> int | None:
            return round(sum(samples) / len(samples)) if samples else None

        def _insert_log() -> None:
            supabase.table("call_logs").insert({
                "student_id": student_id,
                "phone_from": caller_phone or "unknown",
                "intent_detected": _derive_intent(),
                "outcome": _derive_outcome(),
                "escalated": "notify_secretary" in tools_called,
                "duration_seconds": int(time.time() - call_start),
                "avg_response_ms": _avg(latency_response_ms),
                "avg_ttft_ms": _avg(latency_ttft_ms),
                "n_turns": len(latency_response_ms),
            }).execute()

        def _ms(a: float | None, b: float | None) -> int | None:
            return round((b - a) * 1000) if a and b else None

        def _insert_traces() -> None:
            # Per-turn telemetry + per-call rollup with approximate cost.
            call_id = stream_sid or "unknown"
            rows = [
                {
                    "call_id": call_id,
                    "turn_index": i,
                    "ttft_ms": _ms(t.get("t0"), t.get("t_first_token")),
                    "llm_ms": _ms(t.get("t0"), t.get("t_llm_end")),
                    "tts_ttfb_ms": _ms(t.get("t_first_sentence"), t.get("t2")),
                    "tts_ms": _ms(t.get("t_first_sentence"), t.get("t_tts_end")),
                    "response_ms": _ms(t.get("t0"), t.get("t3")),
                    "tool_rounds": t.get("tool_rounds", 0),
                    "tool_ms": round(t.get("tool_ms", 0)),
                    "prompt_tokens": t.get("prompt_tokens", 0),
                    "completion_tokens": t.get("completion_tokens", 0),
                    "tts_chars": t.get("tts_chars", 0),
                }
                for i, t in enumerate(all_turn_timings)
            ]
            if rows:
                supabase.table("turn_metrics").insert(rows).execute()

            total_pt = sum(t.get("prompt_tokens", 0) for t in all_turn_timings)
            total_ct = sum(t.get("completion_tokens", 0) for t in all_turn_timings)
            total_chars = sum(t.get("tts_chars", 0) for t in all_turn_timings)
            duration = int(time.time() - call_start)
            supabase.table("call_traces").insert({
                "call_id": call_id,
                "phone_from": caller_phone or "unknown",
                "student_id": student_id,
                "language": tts_language,
                "duration_seconds": duration,
                "n_turns": len(all_turn_timings),
                "barge_in_count": barge_in_count,
                "total_prompt_tokens": total_pt,
                "total_completion_tokens": total_ct,
                "total_tts_chars": total_chars,
                "cost_usd": call_cost_usd(total_pt, total_ct, total_chars, duration),
            }).execute()

        try:
            await asyncio.to_thread(_insert_log)
            print(f"[log] chiamata registrata — intent={_derive_intent()} duration={int(time.time() - call_start)}s")
        except Exception as exc:
            print(f"[log] errore insert: {exc}")

        try:
            await asyncio.to_thread(_insert_traces)
            print(f"[trace] telemetria registrata — turni={len(all_turn_timings)} barge_in={barge_in_count}")
        except Exception as exc:
            print(f"[trace] errore insert: {exc}")
