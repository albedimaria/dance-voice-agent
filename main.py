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

    dg_connection = deepgram.listen.asynclive.v("1")
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    async def on_transcript(self, result, **kwargs) -> None:
        print(f"[deepgram] callback ricevuto — is_final={result.is_final}")
        transcript = result.channel.alternatives[0].transcript
        if transcript:
            tag = "FINAL" if result.is_final else "partial"
            print(f"[STT {tag}] {transcript}")
        else:
            print("[deepgram] callback con trascrizione vuota")

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

    dg_task = asyncio.create_task(deepgram_sender())

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
        await dg_task
