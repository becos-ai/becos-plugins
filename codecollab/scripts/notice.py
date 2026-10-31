#!/usr/bin/env python3
"""The retired ``codecollab`` plugin: tell the user it is now ``becos``, and do nothing else.

From 0.47.0 the plugin is ``becos@becos``. This stub keeps the old id ``codecollab@becos`` alive for one
release so people who auto-update learn about the move. It captures nothing, recalls nothing, starts no
MCP server and makes no network call, so having it installed beside ``becos`` never double-captures.

* ``session-start``: the notice for the user (``systemMessage``) and one line for the model, so it can
  answer "where did my codecollab commands go?".
* ``prompt-submit``: the same notice, once per session (a session that started before the update
  never saw the SessionStart one). The only thing written is an empty marker file per session under
  the plugin's existing state directory, so the notice is not repeated on every prompt.

Stands down (prints nothing) where the full plugin would: under Cursor, which imports Claude Code hooks
(``CURSOR_VERSION``), and under OpenCode's Claude bridge (``OPENCODE=1`` with ``CLAUDE_AGENT_SDK_VERSION``).
Any failure prints nothing and exits 0: a notice must never break a session.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

NOTICE = ("codecollab is now becos. Run `/plugin uninstall codecollab@becos` and "
          "`/plugin install becos@becos`, then restart Claude Code. Your sign-in and history carry over.")
MODEL_CONTEXT = ("The codecollab plugin is retired and does nothing in this session: no capture, no recall, "
                 "no codecollab tools or commands. It was renamed becos. If the user asks about codecollab, "
                 "team memory or its commands, tell them: " + NOTICE)


def _foreign_host() -> bool:
    env = os.environ
    return bool(env.get("CURSOR_VERSION")) or (env.get("OPENCODE") == "1"
                                               and bool(env.get("CLAUDE_AGENT_SDK_VERSION")))


def _first_prompt_of_session(session_id: str) -> bool:
    """True the first time this session submits a prompt (and when no marker can be kept)."""
    if not session_id:
        return True
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    directory = os.path.join(base, "codecollab", "legacy-notice")
    marker = os.path.join(directory, hashlib.sha256(session_id.encode()).hexdigest()[:32])
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with open(marker, "x", encoding="utf-8"):
            pass
    except FileExistsError:
        return False
    except OSError:
        return True
    return True


def output(event: str, payload: dict) -> dict | None:
    """The hook output for ``event`` (``session-start`` | ``prompt-submit``), or None for silence."""
    if _foreign_host():
        return None
    if event == "session-start":
        return {"systemMessage": NOTICE,
                "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": MODEL_CONTEXT}}
    if event == "prompt-submit":
        session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
        if _first_prompt_of_session(session_id):
            return {"systemMessage": NOTICE}
    return None


def main(argv: list[str]) -> int:
    try:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = {}
        out = output(argv[1] if len(argv) > 1 else "", payload if isinstance(payload, dict) else {})
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 — a notice must never break a session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
