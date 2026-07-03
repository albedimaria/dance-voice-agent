"""TTS latency micro-benchmark: measures what the decision-layer evals can't.

Compares ElevenLabs TTS configs on the metrics that matter for the phone loop:
TTFB (time to first audio chunk — what the caller perceives as "the agent
started talking") and total synthesis time, for the same fixed sentences.

Configs compared:
  A) legacy    : eleven_v3       + pcm_24000  (needs 24k->8k resample + lin2ulaw)
  B) HTTP      : eleven_flash_v2_5 + ulaw_8000 (Twilio-native, no resample)
  C) production: same as B, over the multi-context WebSocket with auto_mode
     (one warm connection, one context per sentence — mirrors main.py)

Run from the repo root:  python -m evals.tts_bench
Cost: ~N_REPS * len(SENTENCES) * ~chars per config — a few hundred chars total.
"""

import asyncio
import base64
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from elevenlabs.client import ElevenLabs
from websockets.asyncio.client import connect as ws_connect

eleven = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
VOICE_ID = os.environ["ELEVENLABS_VOICE_ID"]

N_REPS = 3
LANGUAGE = "it"
# Realistic agent utterances (same register as prompt.py responses).
SENTENCES = [
    "Certo! La prova gratuita di salsa è mercoledì alle diciannove, ti va bene?",
    "Il corso di bachata intermedio costa cinquanta euro al mese, due lezioni a settimana.",
]

CONFIGS = {
    "legacy_v3_pcm24k": {"model_id": "eleven_v3", "output_format": "pcm_24000"},
    "flash_v2_5_ulaw8k": {"model_id": "eleven_flash_v2_5", "output_format": "ulaw_8000"},
}


def bench_once(text: str, model_id: str, output_format: str) -> dict:
    t0 = time.perf_counter()
    ttfb = None
    total_bytes = 0
    for chunk in eleven.text_to_speech.convert_as_stream(
        voice_id=VOICE_ID,
        text=text,
        model_id=model_id,
        output_format=output_format,
        language_code=LANGUAGE,
    ):
        if chunk:
            if ttfb is None:
                ttfb = (time.perf_counter() - t0) * 1000
            total_bytes += len(chunk)
    return {
        "ttfb_ms": round(ttfb or -1, 1),
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
        "bytes": total_bytes,
        "chars": len(text),
    }


async def bench_ws() -> list[dict]:
    """Warm multi-context WebSocket with auto_mode — the production path."""
    url = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/multi-stream-input"
        f"?model_id=eleven_flash_v2_5&output_format=ulaw_8000"
        f"&auto_mode=true&language_code={LANGUAGE}"
    )
    runs: list[dict] = []
    async with ws_connect(
        url,
        additional_headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        open_timeout=10.0,
    ) as ws:
        ctx = 0
        for rep in range(N_REPS):
            for sentence in SENTENCES:
                ctx += 1
                cid = f"b{ctx}"
                t0 = time.perf_counter()
                await ws.send(json.dumps({"text": sentence + " ", "context_id": cid}))
                await ws.send(json.dumps({"context_id": cid, "close_context": True}))
                ttfb = None
                total_bytes = 0
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=15.0))
                    got = msg.get("contextId") or msg.get("context_id")
                    if got is not None and got != cid:
                        continue
                    if msg.get("audio"):
                        if ttfb is None:
                            ttfb = (time.perf_counter() - t0) * 1000
                        total_bytes += len(base64.b64decode(msg["audio"]))
                    if msg.get("isFinal") or msg.get("is_final"):
                        break
                r = {
                    "ttfb_ms": round(ttfb or -1, 1),
                    "total_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "bytes": total_bytes,
                    "chars": len(sentence),
                }
                runs.append(r)
                print(f"[flash_ws_automode] rep{rep} ttfb={r['ttfb_ms']}ms total={r['total_ms']}ms bytes={r['bytes']}")
        await ws.send(json.dumps({"close_socket": True}))
    return runs


def main() -> None:
    results: dict[str, list[dict]] = {name: [] for name in CONFIGS}
    for name, cfg in CONFIGS.items():
        for rep in range(N_REPS):
            for sentence in SENTENCES:
                r = bench_once(sentence, **cfg)
                results[name].append(r)
                print(f"[{name}] rep{rep} ttfb={r['ttfb_ms']}ms total={r['total_ms']}ms bytes={r['bytes']}")
    results["flash_ws_automode"] = asyncio.run(bench_ws())

    print("\n=== summary (median over runs) ===")
    summary = {}
    for name, runs in results.items():
        med_ttfb = statistics.median(r["ttfb_ms"] for r in runs)
        med_total = statistics.median(r["total_ms"] for r in runs)
        summary[name] = {"median_ttfb_ms": med_ttfb, "median_total_ms": med_total, "runs": len(runs)}
        print(f"{name:22s} ttfb={med_ttfb:.0f}ms total={med_total:.0f}ms (n={len(runs)})")

    out = os.path.join(os.path.dirname(__file__), "tts_bench_results.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "runs": results}, f, indent=2)
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
