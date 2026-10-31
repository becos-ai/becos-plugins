#!/usr/bin/env python3
"""Claude Code PreToolUse gate: a host-enforced approval prompt for every mutating becos action.

Live-verified on Claude Code 2.1.292 (2026-10-23): a PreToolUse ``permissionDecision: "ask"`` makes
Claude Code ask the user before the tool runs; in a non-interactive run, where nobody can answer, the
call is denied; and ``--permission-mode bypassPermissions`` does NOT skip it. That supersedes the
earlier "command convention" (docs/DECISIONS.md, 2026-10-17): the same strength of gate OpenCode
enforces, on the same actions — send, accept, decline, close, download, claim a handle, re-file the
session's project, deliver a backfill.

Two routes reach those actions, and both are gated:
  * the bundled MCP server's split tools (``tool_specs.MUTATING``), whatever namespace the host gives
    them (``mcp__plugin_becos_becos__becos_share_send``); a pre-rename
    ``codecollab_*`` name is gated as its ``becos_*`` successor;
  * the core scripts run through Bash — ``shared_handoff.py command-json`` with a mutating command,
    ``project.py … use``, ``backfill.py … --deliver`` — which older command text used.

Anything else gets no opinion (no output), so the user's own permission rules stay in charge. A
gate failure prints nothing, which leaves the host's normal permission flow in place.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tool_specs  # noqa: E402

_SHARED_MUTATIONS = re.compile(r'"command"\s*:\s*"(confirm|accept|decline|close|download)"')
_CLAIM = re.compile(r'"command"\s*:\s*"handles"[^}]*"action"\s*:\s*"claim"|"action"\s*:\s*"claim"[^}]*"command"\s*:\s*"handles"')
_REASONS = {
    "becos_share_send": "Send this becos handoff to the teammate (uploads its files)",
    "becos_shared_accept": "Pick up this becos team handoff",
    "becos_shared_decline": "Record a decline of this becos team handoff",
    "becos_shared_close": "Mark this becos team handoff closed (done)",
    "becos_shared_download": "Get a download link for this becos handoff artifact",
    "becos_handle_claim": "Claim or change your becos handle",
    "becos_project_use": "Re-file where this conversation's work is saved in becos",
    "becos_backfill_deliver": "Ingest past sessions into becos team memory",
}


def _mcp_action(tool: str) -> str | None:
    tail = re.split(r"__|[.:]", tool)[-1]
    tail = re.sub(r"^codecollab_", "becos_", tail)   # tools were codecollab_* before 0.47.0
    return tail if tail in tool_specs.MUTATING else None


def _bash_action(command: str) -> str | None:
    if "shared_handoff.py" in command and "command-json" in command:
        if (m := _SHARED_MUTATIONS.search(command)):
            return {"confirm": "becos_share_send", "accept": "becos_shared_accept",
                    "decline": "becos_shared_decline", "close": "becos_shared_close",
                    "download": "becos_shared_download"}[m.group(1)]
        if _CLAIM.search(command):
            return "becos_handle_claim"
    if "project.py" in command and re.search(r"\buse\b", command):
        return "becos_project_use"
    if "backfill.py" in command and "--deliver" in command:
        return "becos_backfill_deliver"
    return None


def decide(payload: dict) -> dict | None:
    tool = payload.get("tool_name")
    inp = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if not isinstance(tool, str):
        return None
    action = _bash_action(str(inp.get("command") or "")) if tool == "Bash" else _mcp_action(tool)
    if not action:
        return None
    detail = next((f" ({k}: {inp[k]})" for k in ("handoff_id", "handle", "name", "session", "project")
                   if isinstance(inp.get(k), str) and inp[k].strip()), "")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                   "permissionDecisionReason": f"[becos] {_REASONS[action]}{detail}?"}}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        out = decide(payload) if isinstance(payload, dict) else None
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 — no output keeps the host's own permission flow
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
