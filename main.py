from dotenv import load_dotenv

load_dotenv()

import os

import asyncio
import audioop
import base64
import json
import time
import traceback

import httpx

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from supabase import create_client, Client
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from openai import AsyncOpenAI

from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from prompt import SYSTEM_PROMPT
from tools.supabase_tools import get_student_by_phone, get_courses, create_booking, create_recovery, notify_secretary, get_settings, check_trial_used, create_trial_session, get_pricing

app = FastAPI(title="dance-voice-agent")

supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)

twilio = TwilioClient(
    os.environ["TWILIO_ACCOUNT_SID"],
    os.environ["TWILIO_AUTH_TOKEN"],
)
tw_validator = RequestValidator(os.environ["TWILIO_AUTH_TOKEN"])

deepgram = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])

openai = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])


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
                        "description": "Filtra per sede (es. 'Milano Centro', 'Navigli').",
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
    stream_url = f"wss://{host}/media-stream"

    response = VoiceResponse()
    connect = Connect()
    stream = Stream(url=stream_url)
    stream.parameter(name="from", value=form.get("From", ""))
    connect.append(stream)
    response.append(connect)

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    print("[stream] WebSocket accettato")

    stream_sid: str = ""
    caller_phone: str = ""
    dg_connection = deepgram.listen.asynclive.v("1")
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    llm_queue: asyncio.Queue[str | None] = asyncio.Queue()
    tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
    history: list[dict] = []
    is_speaking: bool = False
    call_start: float = time.time()
    student_id: str | None = None
    tools_called: set[str] = set()

    async def _barge_in() -> None:
        nonlocal is_speaking
        if not is_speaking:
            return
        is_speaking = False
        while not tts_queue.empty():
            tts_queue.get_nowait()
        await websocket.send_text(json.dumps({
            "event": "clear",
            "streamSid": stream_sid,
        }))
        print("[barge-in] TTS interrotto")

    async def on_speech_started(result, **kwargs) -> None:
        print("[VAD] parlato rilevato")
        await _barge_in()

    async def on_transcript(result, **kwargs) -> None:
        transcript = result.channel.alternatives[0].transcript
        if not transcript:
            return
        if result.is_final:
            print(f"[STT FINAL] {transcript}")
            await llm_queue.put(transcript)
        else:
            print(f"[STT partial] {transcript}")
            await _barge_in()  # fallback se SpeechStarted non ha già triggerato

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
        while True:
            text = await llm_queue.get()
            if text is None:
                break
            print(f"[LLM] input: {text}")
            history.append({"role": "user", "content": text})
            try:
                messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
                max_iterations = 10
                for _ in range(max_iterations):
                    response = await asyncio.wait_for(
                        openai.chat.completions.create(
                            model="gpt-4o-mini",
                            messages=messages,
                            tools=OPENAI_TOOLS,
                            tool_choice="auto",
                        ),
                        timeout=10.0,
                    )
                    msg = response.choices[0].message
                    messages.append(msg)

                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            fn = tc.function.name
                            args = json.loads(tc.function.arguments)
                            print(f"[LLM] tool call: {fn}({args})")
                            tools_called.add(fn)
                            if fn == "get_courses":
                                result = await get_courses(supabase, **args)
                            elif fn == "create_booking":
                                result = await create_booking(supabase, **args)
                            elif fn == "create_recovery":
                                result = await create_recovery(supabase, **args)
                            elif fn == "notify_secretary":
                                result = await notify_secretary(
                                    caller_phone=caller_phone, twilio_client=twilio, **args
                                )
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
                            print(f"[LLM] tool result: {result}")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            })
                    else:
                        reply = (msg.content or "").strip()
                        history.append({"role": "assistant", "content": reply})
                        print(f"[LLM] risposta: {reply}")
                        if reply:
                            await tts_queue.put(reply)
                        break
            except Exception:
                print(f"[LLM] errore:\n{traceback.format_exc()}")
                history.pop()

    _el_api_key = os.environ["ELEVENLABS_API_KEY"]
    _el_voice_id = os.environ["ELEVENLABS_VOICE_ID"]
    _el_url = f"https://api.elevenlabs.io/v1/text-to-speech/{_el_voice_id}/stream"
    _el_headers = {
        "xi-api-key": _el_api_key,
        "Content-Type": "application/json",
        "Accept": "audio/basic",  # mulaw
    }

    async def tts_sender() -> None:
        nonlocal is_speaking
        FRAME = 160  # 20ms @ mulaw 8kHz

        async def _send_frame(data: bytes) -> None:
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(data).decode()},
            }))

        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)) as client:
            while True:
                text = await tts_queue.get()
                if text is None:
                    break
                print(f"[TTS] sintetizzando: {text}")
                try:
                    buf = b""
                    is_speaking = True
                    async with client.stream(
                        "POST",
                        _el_url,
                        headers=_el_headers,
                        json={
                            "text": text,
                            "model_id": "eleven_turbo_v2_5",
                            "output_format": "ulaw_8000",
                        },
                    ) as resp:
                        print(f"[TTS] ElevenLabs HTTP {resp.status_code}")
                        if resp.status_code != 200:
                            body = await resp.aread()
                            print(f"[TTS] ElevenLabs errore body: {body.decode(errors='replace')}")
                            continue
                        async for chunk in resp.aiter_bytes(chunk_size=4096):
                            if not is_speaking:
                                break
                            if not chunk:
                                continue
                            # output is already mulaw 8kHz — no conversion needed
                            buf += chunk
                            while len(buf) >= FRAME:
                                if not is_speaking:
                                    break
                                await _send_frame(buf[:FRAME])
                                buf = buf[FRAME:]
                    if is_speaking and buf:
                        await _send_frame(buf)
                except Exception as exc:
                    print(f"[TTS] errore: {traceback.format_exc()}")
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
                        print(f"[DB] studente: {student['first_name']} {student['last_name']} ({student['level']})")
                        history.append({
                            "role": "system",
                            "content": (
                                f"Stai parlando con {student['first_name']} {student['last_name']}, "
                                f"livello {student['level']}. "
                                f"Abbonamento attivo: {'sì' if student['active_subscription'] else 'no'}."
                            ),
                        })
                    else:
                        print(f"[DB] numero non trovato: {caller_phone}")
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
