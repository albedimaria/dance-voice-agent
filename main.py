from dotenv import load_dotenv

load_dotenv()

import os

import asyncio
import audioop
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
from elevenlabs import ElevenLabs
from prompt import SYSTEM_PROMPT
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
eleven = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])

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


OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_courses",
            "description": (
                "Recupera i corsi attivi di Ritmo Caliente. "
                "Usa questo tool per verificare disponibilità prima di confermare prenotazioni o recuperi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "level": {
                        "type": "string",
                        "enum": ["base", "intermedio", "avanzato"],
                        "description": "Filtra per livello del corso.",
                    },
                    "location": {
                        "type": "string",
                        "description": "Filtra per sede (es. 'AIDA', 'TIGER').",
                    },
                    "instructor": {
                        "type": "string",
                        "description": "Filtra per nome istruttore (ricerca parziale, es. 'Marco', 'Rossi').",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": (
                "Prenota una lezione regolare per uno studente. "
                "Chiama SOLO dopo aver confermato corso e data con il chiamante. "
                "Verifica prima la disponibilità con get_courses."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente (da get_student_by_phone).",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso (da get_courses).",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data della lezione in formato YYYY-MM-DD.",
                    },
                },
                "required": ["student_id", "course_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_recovery",
            "description": (
                "Prenota un recupero per uno studente in un corso di livello inferiore. "
                "Il sistema verifica automaticamente la compatibilità di livello e la capienza. "
                "Chiama SOLO dopo aver confermato corso e data con il chiamante."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente (da get_student_by_phone).",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso target (da get_courses).",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data del recupero in formato YYYY-MM-DD.",
                    },
                },
                "required": ["student_id", "course_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify_secretary",
            "description": (
                "Invia un messaggio WhatsApp alla segreteria di Ritmo Caliente. "
                "Usa questo tool quando il chiamante ha un problema che non riesci a risolvere autonomamente "
                "(es. reclami, richieste speciali, pagamenti, situazioni fuori dalla tua competenza)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Descrizione chiara del problema o della richiesta del chiamante.",
                    },
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_settings",
            "description": (
                "Legge le impostazioni globali della scuola (es. 'trial_week_active'). "
                "Usalo per verificare se la settimana di prova è attiva prima di proporre lezioni di prova."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_trial_used",
            "description": (
                "Verifica se uno studente ha già usato la lezione di prova per un corso specifico. "
                "Restituisce true se la prova è già stata usata, false altrimenti."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente.",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso.",
                    },
                },
                "required": ["student_id", "course_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_trial_session",
            "description": (
                "Registra una lezione di prova per uno studente in un corso. "
                "Chiama solo se trial_week_active è true e check_trial_used ha restituito false. "
                "Ogni studente può fare al massimo una prova per corso."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "student_id": {
                        "type": "string",
                        "description": "UUID dello studente.",
                    },
                    "course_id": {
                        "type": "string",
                        "description": "UUID del corso.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Data della lezione di prova in formato YYYY-MM-DD.",
                    },
                },
                "required": ["student_id", "course_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pricing",
            "description": (
                "Calcola il costo dell'abbonamento in base al numero di corsi. "
                "Primo corso €160, ogni corso aggiuntivo €128 (−20%). "
                "Usa quando il chiamante chiede informazioni sui prezzi."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "course_count": {
                        "type": "integer",
                        "description": "Numero di corsi a cui lo studente vuole iscriversi.",
                    },
                },
                "required": ["course_count"],
            },
        },
    },
]


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
    stream_url = f"wss://{host}/media-stream?token={token}"

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=stream_url)
    stream.parameter(name="from", value=form.get("From", ""))
    connect.append(stream)
    response.append(connect)

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token", "")
    if not _verify_ws_token(token):
        await websocket.accept()
        await websocket.close(code=1008)
        print("[stream] WebSocket rifiutato — token mancante o scaduto")
        return
    await websocket.accept()
    print("[stream] WebSocket accettato")

    stream_sid: str = ""
    caller_phone: str = ""
    tts_language: str = "it"  # updated from student.language_preference on call start
    dg_connection = deepgram.listen.asynclive.v("1")
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    llm_queue: asyncio.Queue[str | None] = asyncio.Queue()
    tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
    history: list[dict] = []
    is_speaking: bool = False
    llm_busy: bool = False
    call_start: float = time.time()
    student_id: str | None = None
    tools_called: set[str] = set()
    last_barge_in_time: float = 0.0
    BARGE_IN_COOLDOWN: float = 0.8

    async def _barge_in() -> None:
        nonlocal is_speaking, last_barge_in_time
        if not is_speaking:
            return
        is_speaking = False
        last_barge_in_time = time.time()
        while not tts_queue.empty():
            tts_queue.get_nowait()
        await websocket.send_text(json.dumps({
            "event": "clear",
            "streamSid": stream_sid,
        }))
        print("[barge-in] TTS interrotto")

    async def on_speech_started(*args, **kwargs) -> None:
        print("[VAD] parlato rilevato")

    async def on_transcript(*args, **kwargs) -> None:
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
                print(f"[STT FINAL] {transcript}")
                while not llm_queue.empty():
                    try:
                        llm_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await llm_queue.put(transcript)
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
            else:
                result = {"error": f"tool {fn!r} non implementato"}
            print(f"[LLM] tool result ({fn}): {result}")
            return tc_id, result

        while True:
            text = await llm_queue.get()
            if text is None:
                break
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
                        ),
                        timeout=10.0,
                    )

                    async for chunk in stream:
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
                            text_acc += delta.content
                            sentence_buf += delta.content
                            while True:
                                m = sentence_re.search(sentence_buf)
                                if not m:
                                    break
                                sentence = sentence_buf[:m.start() + 1].strip()
                                sentence_buf = sentence_buf[m.end():]
                                if sentence:
                                    await tts_queue.put(sentence)

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
                        results = await asyncio.gather(*[
                            _dispatch_tool(tc["id"], tc["name"], tc["arguments"])
                            for tc in tool_calls_acc.values()
                        ])
                        for tc_id, result in results:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                    else:
                        if sentence_buf.strip():
                            await tts_queue.put(sentence_buf.strip())
                        history.append({"role": "assistant", "content": text_acc})
                        print(f"[LLM] risposta: {text_acc}")
                        break
                else:
                    fallback = "Mi dispiace, ho avuto un problema tecnico. Riprova o contatta la segreteria."
                    await tts_queue.put(fallback)
                    history.append({"role": "assistant", "content": fallback})
                    print("[LLM] limite iterazioni raggiunto — fallback inviato")

            except Exception:
                print(f"[LLM] errore:\n{traceback.format_exc()}")
                history.pop()
            finally:
                llm_busy = False

    async def tts_sender() -> None:
        nonlocal is_speaking
        FRAME = 160  # 20ms @ mulaw 8kHz
        loop = asyncio.get_running_loop()

        async def _send_frame(data: bytes) -> None:
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(data).decode()},
            }))

        while True:
            text = await tts_queue.get()
            if text is None:
                break
            print(f"[TTS] sintetizzando: {text} (lang={tts_language})")
            try:
                buf = b""
                ratecv_state = None
                is_speaking = True

                # Bridge: run sync ElevenLabs generator in a thread and feed
                # PCM chunks into an async queue for the event loop to consume.
                chunk_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

                def _generate_sync() -> None:
                    try:
                        for chunk in eleven.text_to_speech.convert_as_stream(
                            voice_id=os.environ["ELEVENLABS_VOICE_ID"],
                            text=text,
                            model_id="eleven_turbo_v2_5",
                            output_format="pcm_24000",
                            language_code=tts_language,
                        ):
                            if chunk:
                                asyncio.run_coroutine_threadsafe(
                                    chunk_queue.put(chunk), loop
                                ).result()
                    finally:
                        asyncio.run_coroutine_threadsafe(
                            chunk_queue.put(None), loop
                        ).result()

                generate_future = loop.run_in_executor(None, _generate_sync)

                try:
                    while True:
                        pcm_chunk = await asyncio.wait_for(chunk_queue.get(), timeout=15.0)
                        if pcm_chunk is None:
                            break
                        if not is_speaking:
                            break
                        resampled, ratecv_state = audioop.ratecv(
                            pcm_chunk, 2, 1, 24000, 8000, ratecv_state
                        )
                        mulaw = audioop.lin2ulaw(resampled, 2)
                        buf += mulaw
                        while len(buf) >= FRAME:
                            if not is_speaking:
                                break
                            await _send_frame(buf[:FRAME])
                            buf = buf[FRAME:]
                    if is_speaking and buf:
                        await _send_frame(buf)
                finally:
                    await generate_future  # attendi che il thread termini

            except Exception as exc:
                print(f"[TTS] errore: {exc}")
            finally:
                is_speaking = False

    dg_task = asyncio.create_task(deepgram_sender())
    llm_task = asyncio.create_task(llm_worker())
    tts_task = asyncio.create_task(tts_sender())

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
                caller_phone = data["start"].get("customParameters", {}).get("from", "")
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
                await tts_queue.put(
                    "Ciao! Sono TropicoCHETA, l'assistente di Ritmo Caliente. Come posso aiutarti?"
                )
            elif event == "stop":
                print("[stream] terminato")
                break
            else:
                print(f"[twilio] evento sconosciuto: {event}")
    except Exception as exc:
        print(f"[stream] errore ricezione: {exc}")
    finally:
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

        def _insert_log() -> None:
            supabase.table("call_logs").insert({
                "student_id": student_id,
                "phone_from": caller_phone or "unknown",
                "intent_detected": _derive_intent(),
                "outcome": _derive_outcome(),
                "escalated": "notify_secretary" in tools_called,
                "duration_seconds": int(time.time() - call_start),
            }).execute()

        try:
            await asyncio.to_thread(_insert_log)
            print(f"[log] chiamata registrata — intent={_derive_intent()} duration={int(time.time() - call_start)}s")
        except Exception as exc:
            print(f"[log] errore insert: {exc}")
