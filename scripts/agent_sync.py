"""Sync the ConvAI agent config between ElevenLabs and this repo.

The agent's prompt used to live ONLY on ElevenLabs: no diff, no rollback, no
review. This script makes the repo the source of truth.

    python scripts/agent_sync.py pull            # ElevenLabs -> agent/
    python scripts/agent_sync.py diff            # what differs, live vs repo
    python scripts/agent_sync.py push --yes      # agent/ -> ElevenLabs (prompt + LLM knobs)

`push` writes to the LIVE agent of a real customer. It always snapshots the
current live config to agent/backups/ first, refuses to run without --yes, and
only sends the fields it owns (prompt, llm, temperature) — everything else on
the platform stays untouched.

Files it owns:
    agent/prompt.md              system prompt (source of truth)
    agent/agent.json             LLM knobs + the config fields worth reviewing
    agent/config.snapshot.json   full live config, for rollback and blame
    agent/tools/<name>.json      the server tools, one file each
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

API = "https://api.elevenlabs.io"
ROOT = Path(__file__).resolve().parent.parent
AGENT_DIR = ROOT / "agent"
PROMPT_FILE = AGENT_DIR / "prompt.md"
AGENT_FILE = AGENT_DIR / "agent.json"
SNAPSHOT_FILE = AGENT_DIR / "config.snapshot.json"
TOOLS_DIR = AGENT_DIR / "tools"
BACKUP_DIR = AGENT_DIR / "backups"

# Fields this script owns on push. Anything not listed here is platform config
# (voice, turn-taking, evaluation criteria, webhooks) and is only ever read.
OWNED = ("prompt", "llm", "temperature")


def _key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY mancante (.env)")
    return key


def _agent_id() -> str:
    return os.environ.get("ELEVENLABS_AGENT_ID", "agent_9001ks7e78grfazrhck2vfmy2kcr")


def _call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}",
        data=data,
        method=method,
        headers={"xi-api-key": _key(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:400]}")


def fetch_live() -> tuple[dict, list[dict]]:
    cfg = _call("GET", f"/v1/convai/agents/{_agent_id()}")
    tool_ids = cfg["conversation_config"]["agent"]["prompt"].get("tool_ids", [])
    tools = [_call("GET", f"/v1/convai/tools/{tid}") for tid in tool_ids]
    return cfg, tools


def _prompt_of(cfg: dict) -> str:
    return cfg["conversation_config"]["agent"]["prompt"]["prompt"]


def _agent_view(cfg: dict) -> dict:
    """The reviewable slice: what a human should read in a PR."""
    p = cfg["conversation_config"]["agent"]["prompt"]
    a = cfg["conversation_config"]["agent"]
    return {
        "name": cfg.get("name"),
        "agent_id": cfg.get("agent_id"),
        "llm": p.get("llm"),
        "temperature": p.get("temperature"),
        "first_message": a.get("first_message"),
        "language": a.get("language"),
        "dynamic_variable_placeholders": (
            a.get("dynamic_variables", {}).get("dynamic_variable_placeholders", {})
        ),
        "tools": sorted(t["tool_config"]["name"] for t in _cached_tools()),
        "prompt_chars": len(p.get("prompt", "")),
    }


_TOOLS_CACHE: list[dict] = []


def _cached_tools() -> list[dict]:
    return _TOOLS_CACHE


def cmd_pull() -> None:
    global _TOOLS_CACHE
    cfg, tools = fetch_live()
    _TOOLS_CACHE = tools

    AGENT_DIR.mkdir(exist_ok=True)
    TOOLS_DIR.mkdir(exist_ok=True)

    PROMPT_FILE.write_text(_prompt_of(cfg), encoding="utf-8")
    SNAPSHOT_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    AGENT_FILE.write_text(json.dumps(_agent_view(cfg), indent=2, ensure_ascii=False), encoding="utf-8")

    for old in TOOLS_DIR.glob("*.json"):
        old.unlink()
    for t in tools:
        name = t["tool_config"]["name"]
        (TOOLS_DIR / f"{name}.json").write_text(
            json.dumps(t, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(f"pull ok — prompt {len(_prompt_of(cfg))} char, {len(tools)} tool")


def cmd_diff() -> None:
    global _TOOLS_CACHE
    cfg, tools = fetch_live()
    _TOOLS_CACHE = tools

    if not PROMPT_FILE.exists():
        sys.exit("agent/prompt.md non esiste — lancia prima: python scripts/agent_sync.py pull")

    live_prompt = _prompt_of(cfg)
    repo_prompt = PROMPT_FILE.read_text(encoding="utf-8")

    if live_prompt == repo_prompt:
        print("prompt: identico (live == repo)")
    else:
        print("prompt: DIVERSO — live vs repo")
        for line in difflib.unified_diff(
            repo_prompt.splitlines(), live_prompt.splitlines(),
            fromfile="repo/agent/prompt.md", tofile="live/elevenlabs", lineterm="",
        ):
            print(line)

    live_view = _agent_view(cfg)
    repo_view = json.loads(AGENT_FILE.read_text(encoding="utf-8")) if AGENT_FILE.exists() else {}
    drift = {k: (repo_view.get(k), v) for k, v in live_view.items() if repo_view.get(k) != v}
    if drift:
        print("\nconfig: DIVERSA (repo -> live)")
        for k, (repo_v, live_v) in drift.items():
            print(f"  {k}: {repo_v!r} -> {live_v!r}")
    else:
        print("config: identica")


def cmd_push(yes: bool) -> None:
    global _TOOLS_CACHE
    if not yes:
        sys.exit("push modifica l'agente LIVE di un cliente vero. Ripeti con --yes.")
    if not PROMPT_FILE.exists() or not AGENT_FILE.exists():
        sys.exit("mancano agent/prompt.md o agent/agent.json — lancia prima: pull")

    cfg, tools = fetch_live()
    _TOOLS_CACHE = tools

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"config-{stamp}.json"
    backup.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"backup pre-push: {backup.relative_to(ROOT)}")

    repo_view = json.loads(AGENT_FILE.read_text(encoding="utf-8"))
    payload = {
        "conversation_config": {
            "agent": {
                "prompt": {
                    "prompt": PROMPT_FILE.read_text(encoding="utf-8"),
                    "llm": repo_view["llm"],
                    "temperature": repo_view["temperature"],
                }
            }
        }
    }
    _call("PATCH", f"/v1/convai/agents/{_agent_id()}", payload)

    live_after = _prompt_of(_call("GET", f"/v1/convai/agents/{_agent_id()}"))
    if live_after != PROMPT_FILE.read_text(encoding="utf-8"):
        sys.exit("push FALLITO: il prompt live non corrisponde al file dopo il PATCH")
    print(f"push ok — prompt {len(live_after)} char live, campi inviati: {', '.join(OWNED)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=("pull", "diff", "push"))
    ap.add_argument("--yes", action="store_true", help="conferma esplicita per push")
    args = ap.parse_args()

    if args.command == "pull":
        cmd_pull()
    elif args.command == "diff":
        cmd_diff()
    else:
        cmd_push(args.yes)


if __name__ == "__main__":
    main()
