#!/usr/bin/env python3
"""Opt-in local audit of the model's ``vonic_resolve_event`` calls, for every client.

The model resolves a recalled citation by calling ``vonic_resolve_event`` on the configured MCP
surface — not through this plugin's scripts — so ``gbrain_client``'s own audit never sees it. Each
adapter therefore records the call AFTER it completed, from the host's post-tool hook. Two
independent files under the cache root, the format the OpenCode adapter has written since V1
(``recordResolveCall``):

  backend-api.log  (``VONIC_BACKEND_AUDIT_LOG=1``)  the request/response pair, one JSON line
  resolve.log      (``VONIC_CODECOLLAB_DEBUG=1``)   one compact human line per resolve

Hosts may namespace an MCP tool id (``mcp__becos__vonic_resolve_event``, ``MCP:vonic_resolve_event``,
``becos.vonic_resolve_event``), so the match is on the trailing segment. Nothing here can change the
tool's result, and every failure is swallowed.

Hook: ``resolve_audit.py post-tool-use`` reads a Claude Code / Codex PostToolUse payload on stdin and
prints nothing.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

RESOLVE_TOOL = "vonic_resolve_event"
_ERROR_RE = re.compile(r"^\[?error\b", re.I)


def is_resolve_tool(tool: object) -> bool:
    if not isinstance(tool, str):
        return False
    return tool == RESOLVE_TOOL or any(tool.endswith(sep + RESOLVE_TOOL) for sep in ("_", ".", ":"))


def result_text(response: object) -> str:
    """Flatten a tool response (MCP content list, a string, or a JSON value) to its text."""
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        return "".join(part.get("text", "") for part in response if isinstance(part, dict)
                       and isinstance(part.get("text"), str))
    if isinstance(response, dict):
        for key in ("content", "output", "result"):
            if key in response:
                return result_text(response[key])
        return json.dumps(response)
    return "" if response is None else json.dumps(response)


def entries(tool: str, args: object, text: str, session_id: str | None, call_id: str | None,
            source: str, env=os.environ) -> list[tuple[str, str]]:
    """``[(file name, line)]`` to append; pure, so the format is testable without a filesystem."""
    if not is_resolve_tool(tool):
        return []
    tag = env.get("VONIC_CODECOLLAB_CLIENT_TAG") or "cc"
    request = args if isinstance(args, dict) else {}
    now = time.time()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now))
    out: list[tuple[str, str]] = []
    if env.get("VONIC_BACKEND_AUDIT_LOG") == "1":
        record = {"timestamp": f"{stamp}.{int(now * 1000) % 1000:03d}Z", "client_tag": tag,
                  "tool": RESOLVE_TOOL, "request": request,
                  "response": {"content": [{"type": "text", "text": text}]},
                  "session_id": session_id, "call_id": call_id, "resolve_source": source}
        out.append(("backend-api.log", json.dumps(record) + "\n"))
    if env.get("VONIC_CODECOLLAB_DEBUG") == "1":
        compact = " ".join(text.split())
        try:
            limit = int(env.get("VONIC_RESOLVE_LOGCHARS", "500"))
        except ValueError:
            limit = 500
        outcome = "not-found" if not compact or compact == "null" else (
            "failed" if _ERROR_RE.match(compact) else "resolved")
        line = f"{stamp}  {tag}/vplugin  event_id={request.get('event_id', '')} outcome={outcome}"
        out.append(("resolve.log", line + (f"  ::  {compact[:limit]}" if compact else "") + "\n"))
    return out


def record(tool: str, args: object, text: str, session_id: str | None = None,
           call_id: str | None = None, source: str = "post-tool-use") -> None:
    try:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        for name, line in entries(tool, args, text, session_id, call_id, source):
            path = os.path.join(base, "codecollab", name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 — an audit trail must never break the call it records
        pass


def main(argv: list[str]) -> int:
    if (argv[:1] != ["post-tool-use"] or os.environ.get("VONIC_CODECOLLAB_DISABLED") == "1"
            or not (os.environ.get("VONIC_BACKEND_AUDIT_LOG") == "1" or os.environ.get("VONIC_CODECOLLAB_DEBUG") == "1")):
        return 0
    try:
        payload = json.load(sys.stdin)
        if isinstance(payload, dict) and is_resolve_tool(payload.get("tool_name")):
            record(payload["tool_name"], payload.get("tool_input"), result_text(payload.get("tool_response")),
                   payload.get("session_id"), payload.get("tool_use_id"))
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
