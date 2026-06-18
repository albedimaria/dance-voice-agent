"""Approximate per-call cost estimation for the observability dashboard.

These are ROUGH, plan-dependent rates, not real billing — they exist to give a
relative "cost per call" signal. Update the constants to match your contracts.
"""

# GPT-4o (per 1K tokens). Source: OpenAI pricing, early 2025.
GPT4O_INPUT_USD_PER_1K = 0.0025
GPT4O_OUTPUT_USD_PER_1K = 0.0100

# ElevenLabs TTS — billed per character; rate varies a lot by plan. Approx.
ELEVEN_USD_PER_1K_CHARS = 0.30

# Twilio inbound voice + Media Streams, per minute. Approx, region-dependent.
TWILIO_USD_PER_MIN = 0.013


def call_cost_usd(
    prompt_tokens: int,
    completion_tokens: int,
    tts_chars: int,
    duration_seconds: int,
) -> float:
    """Approximate total cost of one call in USD, rounded to 4 decimals."""
    llm = (
        (prompt_tokens / 1000) * GPT4O_INPUT_USD_PER_1K
        + (completion_tokens / 1000) * GPT4O_OUTPUT_USD_PER_1K
    )
    tts = (tts_chars / 1000) * ELEVEN_USD_PER_1K_CHARS
    telephony = (duration_seconds / 60) * TWILIO_USD_PER_MIN
    return round(llm + tts + telephony, 4)
