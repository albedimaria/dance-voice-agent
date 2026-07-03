"""TTS latency micro-benchmark: measures what the decision-layer evals can't.

Compares ElevenLabs TTS configs on the metrics that matter for the phone loop:
TTFB (time to first audio chunk — what the caller perceives as "the agent
started talking") and total synthesis time, for the same fixed sentences.

Configs compared:
  A) legacy    : eleven_v3       + pcm_24000  (needs 24k->8k resample + lin2ulaw)
  B) production: eleven_flash_v2_5 + ulaw_8000 (Twilio-native, no resample)

Run from the repo root:  python -m evals.tts_bench
Cost: ~N_REPS * len(SENTENCES) * ~chars per config — a few hundred chars total.
"""

import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from elevenlabs.client import ElevenLabs

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


def main() -> None:
    results: dict[str, list[dict]] = {name: [] for name in CONFIGS}
    for name, cfg in CONFIGS.items():
        for rep in range(N_REPS):
            for sentence in SENTENCES:
                r = bench_once(sentence, **cfg)
                results[name].append(r)
                print(f"[{name}] rep{rep} ttfb={r['ttfb_ms']}ms total={r['total_ms']}ms bytes={r['bytes']}")

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
