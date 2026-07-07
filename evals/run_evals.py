"""Reproducible eval suite for the voice agent's decision layer.

Runs a fixed set of scenarios through the SAME system prompt (prompt.py), tool
schema (tools_schema.py), tool functions (tools/supabase_tools.py) and model the
production agent uses, and scores task-success = did it call the expected tool.
Read tools hit real Supabase; write
tools (booking/recovery/trial/notify) are intercepted and NOT persisted, so runs
are reproducible and don't pollute the DB.

Writes one `eval_runs` summary row + one `eval_results` row per scenario, so the
dashboard can show success rate, latency p50/p95, and the trend across runs.

Run from the repo root:  python -m evals.run_evals
"""

import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

# Reuse the agent's exact prompt + tool schema + tool functions, but create our
# own light clients — avoids importing the server (Deepgram/ElevenLabs/FastAPI).
from openai import AsyncOpenAI
from supabase import create_client
from prompt import SYSTEM_PROMPT
from tools_schema import OPENAI_TOOLS
from tools.supabase_tools import (
    check_trial_used,
    get_courses,
    get_faq,
    get_pricing,
    get_settings,
)
from pricing import GPT4O_INPUT_USD_PER_1K, GPT4O_OUTPUT_USD_PER_1K, ELEVEN_USD_PER_1K_CHARS

openai_client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
supabase = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_SERVICE_ROLE_KEY"],
)

MODEL = "gpt-4o"
MAX_TOOL_ROUNDS = 10
SCENARIO_DELAY_S = 12  # spacing between scenarios to stay under the OpenAI TPM limit
SCENARIOS_PATH = os.path.join(os.path.dirname(__file__), "scenarios.json")


async def _create(messages: list[dict]):
    """Create a completion, retrying with backoff on rate-limit (429)."""
    for attempt in range(5):
        try:
            return await openai_client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=OPENAI_TOOLS,
                tool_choice="auto",
                stream=False,
            )
        except Exception as exc:
            msg = str(exc).lower()
            if ("rate_limit" in msg or "429" in msg) and attempt < 4:
                await asyncio.sleep(8 * (attempt + 1))
                continue
            raise

# Read-only tools run for real against Supabase; everything else is a side effect
# we intercept (record the decision, return a simulated success) to keep runs pure.
READ_ONLY_TOOLS = {"get_courses", "get_pricing", "get_settings", "check_trial_used", "get_faq"}

# get_student_bookings is a read, but it depends on mutable booking state — a
# canned fixture keeps the cancel scenario reproducible regardless of the DB.
_BOOKINGS_FIXTURE = [
    {
        "booking_id": "c1000000-0000-0000-0000-000000000001",
        "date": "2026-07-15",
        "type": "regular",
        "course_name": "Bachata Sensual Base",
        "time_start": "19:00:00",
        "location": "AIDA",
    }
]


async def _dispatch(fn: str, args: dict, writes: list[str]) -> dict | list:
    if fn == "get_courses":
        return await get_courses(supabase, **args)
    if fn == "get_pricing":
        return get_pricing(**args)
    if fn == "get_settings":
        return await get_settings(supabase)
    if fn == "check_trial_used":
        return await check_trial_used(supabase, **args)
    if fn == "get_faq":
        return await get_faq(supabase, **args)
    if fn == "get_student_bookings":
        return _BOOKINGS_FIXTURE
    # Write / side-effect tools: record and simulate success, no persistence.
    writes.append(fn)
    return {"ok": True, "_stubbed_for_eval": True}


async def run_scenario(sc: dict) -> dict:
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if sc.get("system"):
        messages.append({"role": "system", "content": sc["system"]})
    # A scenario is one or more user turns (multi-turn captures flows like
    # book → confirm → create_booking). `user` is shorthand for a single turn.
    user_turns: list[str] = sc.get("turns") or [sc["user"]]

    tools_called: list[dict] = []
    writes: list[str] = []
    prompt_tokens = completion_tokens = 0
    final_text = ""
    # Per-conversational-turn cost samples (one entry per user_msg answered),
    # so we can estimate real cost/turn from more than one sample call.
    turn_costs: list[float] = []

    t0 = time.perf_counter()
    for user_msg in user_turns:
        messages.append({"role": "user", "content": user_msg})
        turn_prompt_tok = turn_completion_tok = 0
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await _create(messages)
            if resp.usage:
                prompt_tokens += resp.usage.prompt_tokens
                completion_tokens += resp.usage.completion_tokens
                turn_prompt_tok += resp.usage.prompt_tokens
                turn_completion_tok += resp.usage.completion_tokens
            msg = resp.choices[0].message
            if msg.tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                })
                for tc in msg.tool_calls:
                    fn = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except Exception:
                        args = {}
                    tools_called.append({"name": fn, "args": args})
                    result = await _dispatch(fn, args, writes)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
            else:
                final_text = msg.content or ""
                # Keep the assistant turn in context for the next user turn.
                messages.append({"role": "assistant", "content": final_text})
                # Cost of this turn: real LLM tokens + TTS cost estimated from
                # the reply length (no real synthesis happens in eval).
                llm_cost = (
                    (turn_prompt_tok / 1000) * GPT4O_INPUT_USD_PER_1K
                    + (turn_completion_tok / 1000) * GPT4O_OUTPUT_USD_PER_1K
                )
                tts_cost = (len(final_text) / 1000) * ELEVEN_USD_PER_1K_CHARS
                turn_costs.append(round(llm_cost + tts_cost, 5))
                break
    latency_ms = round((time.perf_counter() - t0) * 1000)

    called_names = [t["name"] for t in tools_called]
    expected = sc["expect_tool"]
    expected_list = expected if isinstance(expected, list) else [expected]
    passed = any(e in called_names for e in expected_list)

    return {
        "scenario_id": sc["id"],
        "name": sc["name"],
        "passed": passed,
        "expected": json.dumps(expected, ensure_ascii=False),
        "actual": json.dumps(called_names, ensure_ascii=False),
        "latency_ms": latency_ms,
        "tool_calls": json.dumps(tools_called, ensure_ascii=False),
        "final_text": final_text,
        "turn_costs": turn_costs,
    }


def _percentile(sorted_vals: list[int], p: float) -> int | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return round(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _git_sha() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return None


async def main_async() -> None:
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        scenarios = json.load(f)

    print(f"[eval] running {len(scenarios)} scenarios against {MODEL} (real reads, stubbed writes)\n")
    results = []
    for i, sc in enumerate(scenarios):
        if i > 0:
            await asyncio.sleep(SCENARIO_DELAY_S)  # respect TPM limit
        try:
            r = await run_scenario(sc)
        except Exception as exc:
            print(f"  [ERR ] {sc['name']:<28} {exc}")
            r = {
                "scenario_id": sc["id"], "name": sc["name"], "passed": False,
                "expected": json.dumps(sc.get("expect_tool"), ensure_ascii=False),
                "actual": json.dumps({"error": str(exc)[:200]}, ensure_ascii=False),
                "latency_ms": 0, "tool_calls": "[]", "final_text": "", "turn_costs": [],
            }
        else:
            mark = "PASS" if r["passed"] else "FAIL"
            print(f"  [{mark}] {r['name']:<28} {r['latency_ms']:>6}ms  tools={r['actual']}")
        results.append(r)

    n = len(results)
    n_passed = sum(1 for r in results if r["passed"])
    success_rate = round(100 * n_passed / n, 2) if n else 0.0
    lats = sorted(r["latency_ms"] for r in results)
    avg_ms = round(sum(lats) / len(lats)) if lats else None
    p50 = _percentile(lats, 50)
    p95 = _percentile(lats, 95)

    print(f"\n[eval] {n_passed}/{n} passed ({success_rate}%) — avg {avg_ms}ms · p50 {p50}ms · p95 {p95}ms")

    all_turn_costs = [c for r in results for c in r.get("turn_costs", [])]
    if all_turn_costs:
        avg_turn_cost = sum(all_turn_costs) / len(all_turn_costs)
        print(
            f"[eval] cost/turn: avg ${avg_turn_cost:.4f} (n={len(all_turn_costs)} turns) "
            f"— min ${min(all_turn_costs):.4f} · max ${max(all_turn_costs):.4f}"
        )

    run = supabase.table("eval_runs").insert({
        "git_sha": _git_sha(),
        "model": MODEL,
        "n_scenarios": n,
        "n_passed": n_passed,
        "success_rate": success_rate,
        "avg_response_ms": avg_ms,
        "p50_ms": p50,
        "p95_ms": p95,
        "notes": "real reads, stubbed writes",
    }).execute()
    run_id = run.data[0]["id"]

    supabase.table("eval_results").insert([
        {
            "run_id": run_id,
            "scenario_id": r["scenario_id"],
            "name": r["name"],
            "passed": r["passed"],
            "expected": r["expected"],
            "actual": r["actual"],
            "latency_ms": r["latency_ms"],
            "tool_calls": r["tool_calls"],
        }
        for r in results
    ]).execute()

    print(f"[eval] written eval_runs={run_id} + {n} eval_results")


if __name__ == "__main__":
    asyncio.run(main_async())
