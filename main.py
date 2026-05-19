from dotenv import load_dotenv

load_dotenv()

import os

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

    dg_connection = deepgram.listen.asynclive.v("1")

    async def on_transcript(self, result, **kwargs) -> None:
        transcript = result.channel.alternatives[0].transcript
        if transcript:
            is_final = result.is_final
            tag = "FINAL" if is_final else "partial"
            print(f"[STT {tag}] {transcript}")

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

    await dg_connection.start(options)

    try:
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            event = data.get("event")

            if event == "media":
                audio = base64.b64decode(data["media"]["payload"])
                await dg_connection.send(audio)
            elif event == "start":
                print(f"[stream] avviato — callSid={data['start'].get('callSid')}")
            elif event == "stop":
                print("[stream] terminato")
                break
    except Exception as exc:
        print(f"[stream] errore: {exc}")
    finally:
        await dg_connection.finish()
