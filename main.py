from dotenv import load_dotenv

load_dotenv()

import os

import asyncio
import base64
import json

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from cartesia import AsyncCartesia
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

deepgram = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])
cartesia = AsyncCartesia(api_key=os.environ["CARTESIA_API_KEY"])

CARTESIA_VOICE_ID = "36d94908-c5b9-4014-b521-e69aee5bead0"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/incoming-call")
async def incoming_call(request: Request) -> Response:
    host = request.headers.get("host", request.base_url.hostname)
    stream_url = f"wss://{host}/media-stream"

    response = VoiceResponse()
    response.say(
        "Benvenuto alla scuola di ballo. Come posso aiutarti?",
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
    tts_queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def on_transcript(self, result, **kwargs) -> None:
        transcript = result.channel.alternatives[0].transcript
        if not transcript:
            return
        if result.is_final:
            print(f"[STT FINAL] {transcript}")
            await tts_queue.put(transcript)
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

    async def tts_sender() -> None:
        try:
            while True:
                text = await tts_queue.get()
                if text is None:
                    break
                print(f"[TTS] sintetizzando: {text}")
                async for chunk in cartesia.tts.sse(
                    model_id="sonic-2",
                    transcript=text,
                    voice={"mode": "id", "id": CARTESIA_VOICE_ID},
                    output_format={
                        "container": "raw",
                        "encoding": "pcm_mulaw",
                        "sample_rate": 8000,
                    },
                    language="it",
                ):
                    if chunk.audio:
                        payload = base64.b64encode(chunk.audio).decode()
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": payload},
                        }))
        except Exception as exc:
            print(f"[TTS] errore: {exc}")

    dg_task = asyncio.create_task(deepgram_sender())
    tts_task = asyncio.create_task(tts_sender())

    try:
        while True:
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
        await tts_queue.put(None)
        await dg_task
        await tts_task
