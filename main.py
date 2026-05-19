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
from twilio.twiml.voice_response import VoiceResponse, Connect
import httpx

from openai import AsyncOpenAI

from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions

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

SYSTEM_PROMPT = (
    "Sei un assistente vocale della scuola di ballo Ritmo Caliente. "
    "Rispondi sempre in italiano, con frasi brevi e naturali adatte al parlato. "
    "Non usare elenchi, markdown o simboli speciali. "
    "Sii cordiale e conciso."
)

CARTESIA_VOICE_ID = "36d94908-c5b9-4014-b521-e69aee5bead0"
CARTESIA_API_URL = "https://api.cartesia.ai/tts/sse"


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
    connect.stream(url=stream_url)
    response.append(connect)

    return Response(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    print("[stream] WebSocket accettato")

    stream_sid: str = ""
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
                response = await openai.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": SYSTEM_PROMPT}] + history,
                )
                reply = response.choices[0].message.content.strip()
                history.append({"role": "assistant", "content": reply})
                print(f"[LLM] risposta: {reply}")
                if reply:
                    await tts_queue.put(reply)
            except Exception:
                print(f"[LLM] errore:\n{traceback.format_exc()}")
                history.pop()

    async def tts_sender() -> None:
        headers = {
            "X-API-Key": os.environ["CARTESIA_API_KEY"],
            "Cartesia-Version": "2024-06-10",
            "Content-Type": "application/json",
        }
        body = {
            "model_id": "sonic-2",
            "voice": {"mode": "id", "id": CARTESIA_VOICE_ID},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_mulaw",
                "sample_rate": 8000,
            },
            "language": "it",
        }
        async with httpx.AsyncClient(timeout=30.0) as http:
            try:
                while True:
                    text = await tts_queue.get()
                    if text is None:
                        break
                    print(f"[TTS] sintetizzando: {text}")
                    async with http.stream(
                        "POST", CARTESIA_API_URL,
                        headers=headers,
                        json={**body, "transcript": text},
                    ) as response:
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            chunk = json.loads(line[6:])
                            audio_b64 = chunk.get("audio") or chunk.get("data")
                            if audio_b64:
                                await websocket.send_text(json.dumps({
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": audio_b64},
                                }))
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
                print(f"[stream] avviato — callSid={data['start'].get('callSid')}")
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
