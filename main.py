from dotenv import load_dotenv

load_dotenv()

import os

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from supabase import create_client, Client
from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from deepgram import DeepgramClient

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
    try:
        while True:
            await websocket.receive_text()
    except Exception:
        pass
