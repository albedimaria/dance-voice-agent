from dotenv import load_dotenv

load_dotenv()

import os

import asyncio
import base64
import json
import traceback

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import Response
from supabase import create_client, Client
from twilio.request_validator import RequestValidator
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
import httpx

from openai import AsyncOpenAI

from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from prompt import SYSTEM_PROMPT
from tools.supabase_tools import get_student_by_phone, get_courses, create_booking, create_recovery, notify_secretary

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
    response.say(
        "holaa, qui Ritmo Caliente! la tua chiamata verrà trasferita a un operatore!",
        language="it-IT",
    )
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

    async def on_transcript(self, result, **kwargs) -> None:
        transcript = result.channel.alternatives[0].transcript
        if not transcript:
            return
        if result.is_final:
            print(f"[STT FINAL] {transcript}")
            await llm_queue.put(transcript)
        else:
            print(f"[STT partial] {transcript}")

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
                while True:
                    response = await openai.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=messages,
                        tools=OPENAI_TOOLS,
                        tool_choice="auto",
                    )
                    msg = response.choices[0].message
                    messages.append(msg)

                    if msg.tool_calls:
                        for tc in msg.tool_calls:
                            fn = tc.function.name
                            args = json.loads(tc.function.arguments)
                            print(f"[LLM] tool call: {fn}({args})")
                            if fn == "get_courses":
                                result = await get_courses(supabase, **args)
                            elif fn == "create_booking":
                                result = await create_booking(supabase, **args)
                            elif fn == "create_recovery":
                                result = await create_recovery(supabase, **args)
                            elif fn == "notify_secretary":
                                result = await notify_secretary(caller_phone=caller_phone, **args)
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

    async def tts_sender() -> None:
        voice_id = os.environ["ELEVENLABS_VOICE_ID"]
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        headers = {
            "xi-api-key": os.environ["ELEVENLABS_API_KEY"],
            "Content-Type": "application/json",
        }
        # mulaw 8kHz = 8000 samples/s × 1 byte = 160 bytes per 20ms frame
        FRAME = 160

        async def _send_frame(data: bytes) -> None:
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": stream_sid,
                "media": {"payload": base64.b64encode(data).decode()},
            }))

        async with httpx.AsyncClient(timeout=30.0) as http:
            try:
                while True:
                    text = await tts_queue.get()
                    if text is None:
                        break
                    print(f"[TTS] sintetizzando: {text}")
                    async with http.stream(
                        "POST", url,
                        headers=headers,
                        json={
                            "text": text,
                            "model_id": "eleven_turbo_v2_5",
                            "output_format": "ulaw_8000",
                        },
                    ) as response:
                        if response.status_code != 200:
                            body = await response.aread()
                            print(f"[TTS] errore HTTP {response.status_code}: {body}")
                            continue
                        buf = b""
                        async for chunk in response.aiter_bytes():
                            if not chunk:
                                continue
                            buf += chunk
                            while len(buf) >= FRAME:
                                await _send_frame(buf[:FRAME])
                                buf = buf[FRAME:]
                        if buf:
                            await _send_frame(buf)
            except Exception as exc:
                print(f"[TTS] errore: {exc}")

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
                print(f"[twilio] media — {len(audio)} bytes")
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
