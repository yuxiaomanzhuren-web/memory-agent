"""Claude Code command hooks for memory context and original-message recording."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from uuid import uuid4

from .http.client import HTTPMemoryClient
from .ingest import AgentRequestError

EVENTS = {"session-start": "SessionStart", "user-prompt": "UserPromptSubmit", "stop": "Stop"}


def _context(event, data):
    # Historical text is data. Keep its role, verdict and citation permissions visible.
    return {"hookSpecificOutput": {
        "hookEventName": event,
        "additionalContext": "【记忆库资料，请按来源与状态阅读】\n" + json.dumps(data, ensure_ascii=False),
    }}


def run_hook(event, payload, client):
    if not isinstance(payload, dict) or payload.get("hook_event_name") != event:
        raise AgentRequestError("hook_event_mismatch")
    session = payload.get("session_id")
    if not isinstance(session, str) or session != client.conversation["conversation_id"]:
        raise AgentRequestError("hook_session_mismatch", 403)
    binding = {"conversation": client.conversation}
    if event == "SessionStart":
        recent = client.call("recent", {**binding, "limit": 5})["items"]
        return _context(event, {"recent": [
            {"raw_id": item["id"], "speaker": item["speaker"], "at": item["at"],
             "stance": item["stance"], "support_scope": item["support_scope"],
             "text": item["text"][:1000]} for item in recent
        ]})
    speaker, field = ("user", "prompt") if event == "UserPromptSubmit" else ("assistant", "last_assistant_message")
    text = payload.get(field)
    if event == "Stop" and (text is None or isinstance(text, str) and not text.strip()):
        return None  # A tool-only turn can end with no conversational text.
    written = client.call("remember", {**binding, "text": text, "speaker": speaker, "source_kind": "direct",
                                      "message_id": uuid4().hex, "at": datetime.now(timezone.utc).isoformat()})
    if event == "UserPromptSubmit":
        result = client.call("recall", {**binding, "query": text, "exclude_raw_id": written["raw_id"]})
        return _context(event, {key: result[key] for key in ("state", "items", "missing", "allowed_raw_ids")})
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True, choices=(*EVENTS, *EVENTS.values()))
    name = parser.parse_args().event
    try:
        payload = json.load(sys.stdin.buffer)
        output = run_hook(EVENTS.get(name, name), payload, HTTPMemoryClient.from_env())
    except AgentRequestError as exc:
        reason = "记忆不可用" if exc.status >= 500 else "记忆 hook 未执行"
        print(f"memory-hook: {reason} ({exc.code})", file=sys.stderr)
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("memory-hook: 记忆不可用 (invalid_hook_input_or_response)", file=sys.stderr)
        return 0
    if output is not None:
        print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
