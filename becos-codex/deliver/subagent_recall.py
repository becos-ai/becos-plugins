#!/usr/bin/env python3
"""Request-scoped recall for a subagent the main agent dispatches (Claude Code, Codex).

The OpenCode V2 adapter gives a dispatched subagent the same automatic recall as the primary agent,
searched on the subagent's own task and tagged ``caller_mode: subagent`` with the agent's name.
Claude Code and Codex can do the same, in two steps, because their ``SubagentStart`` hook says WHICH
agent starts (``agent_id``, ``agent_type``) but not WHAT it was asked to do:

  stash  PreToolUse on the dispatch tool (Claude Code ``Agent``/``Task``, Codex ``spawn_agent``):
         queue the task text under the parent session, with the requested agent type.
  start  SubagentStart: claim the oldest queued task for that session (same agent type first),
         run ``recall.py`` on it and return the result as the subagent's ``additionalContext``.

Pairing is FIFO per parent session: hosts start subagents in the order they were dispatched. Two
parallel dispatches of the same type with different tasks could in principle cross; both tasks come
from the same parent turn, so the cost is relevance, never scope. Bounded: at most
``_MAX_ACTIVE`` lookups at once per parent (one dispatch can fan out), queued tasks expire after
``_TTL_S``, and everything fails open — a subagent never waits on this beyond the hook timeout.

Switches: ``VONIC_CODECOLLAB_RECALL_SUBAGENTS=0`` (default on) and
``VONIC_CODECOLLAB_RECALL_INJECT=0`` turn it off, as do the usual disabled/foreign-host guards. A
subagent's recall is never announced to the user and never carries instruction texts.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402

_MAX_ACTIVE = 4          # concurrent subagent lookups per parent session
_MAX_QUEUED = 32
_TTL_S = 300             # a dispatched task older than this is not a pending start any more
_MAX_TASK_CHARS = 20_000
# Dispatch-tool argument names, by host. Claude Code: Agent/Task {prompt, subagent_type,
# description}. Codex: spawn_agent {message|task|prompt, agent_type}. First non-empty wins.
_TASK_KEYS = ("prompt", "message", "task", "instructions", "input", "description")
_TYPE_KEYS = ("subagent_type", "agent_type", "agent_role", "role")


def _enabled() -> bool:
    env = os.environ
    return not (env.get("VONIC_CODECOLLAB_DISABLED") == "1"
                or env.get("VONIC_CODECOLLAB_RECALL_SUBAGENTS", "1") == "0"
                or env.get("VONIC_CODECOLLAB_RECALL_INJECT", "1") == "0"
                or env.get("VONIC_RECALL_ENABLED", "1") != "1"
                or capture.foreign_host() is not None)


def _queue_path(session_id: str) -> str:
    path = os.path.join(capture._cache_dir(), capture._client_tag(), "subagents")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return os.path.join(path, f"{capture._slugify(session_id) or 'session'}.json")


def _first(mapping: dict, keys) -> str:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def stash(payload: dict) -> None:
    session_id = str(payload.get("session_id") or "")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    task = _first(tool_input, _TASK_KEYS)[:_MAX_TASK_CHARS]
    if not session_id or not task:
        return
    path = _queue_path(session_id)
    now = time.time()
    with capture._locked(path):
        state = capture._load_buffer(path) or {}
        queued = [q for q in state.get("queued") or [] if now - q.get("ts", 0) < _TTL_S]
        queued.append({"type": _first(tool_input, _TYPE_KEYS), "task": task, "ts": now})
        state["queued"] = queued[-_MAX_QUEUED:]
        capture._save_buffer(path, state)


def _claim(session_id: str, agent_id: str, agent_type: str) -> str | None:
    """Pop the task for this subagent and take an active slot; None when there is none or no room."""
    path = _queue_path(session_id)
    now = time.time()
    with capture._locked(path):
        state = capture._load_buffer(path) or {}
        queued = [q for q in state.get("queued") or [] if now - q.get("ts", 0) < _TTL_S]
        active = {k: v for k, v in (state.get("active") or {}).items() if now - v < 90}
        index = next((i for i, q in enumerate(queued) if q.get("type") == agent_type), None)
        if index is None and queued:
            index = 0
        task = queued.pop(index)["task"] if index is not None else None
        if task and len(active) >= _MAX_ACTIVE:
            task = None   # fan-out guard: the task is dropped, not deferred — its subagent has started
        elif task:
            active[agent_id or f"anon-{now}"] = now
        state.update(queued=queued, active=active)
        capture._save_buffer(path, state)
    return task


def _release(session_id: str, agent_id: str) -> None:
    path = _queue_path(session_id)
    with capture._locked(path):
        state = capture._load_buffer(path) or {}
        (state.get("active") or {}).pop(agent_id, None)
        capture._save_buffer(path, state)


def start(payload: dict) -> str:
    """The recall hook output for a starting subagent, or "" (inject nothing)."""
    session_id = str(payload.get("session_id") or "")
    agent_id = str(payload.get("agent_id") or "")
    agent_type = str(payload.get("agent_type") or "")
    if not session_id:
        return ""
    task = _claim(session_id, agent_id, agent_type)
    if not task:
        return ""
    request = {"prompt": task, "cwd": payload.get("cwd") or os.getcwd(), "session_id": session_id,
               "caller_mode": "subagent", "recall_source": "subagent-start",
               "hook_event_name": "SubagentStart", **({"caller_agent": agent_type} if agent_type else {})}
    try:
        done = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall.py")],
                              input=json.dumps(request), capture_output=True, text=True, check=False,
                              timeout=float(os.environ.get("VONIC_RECALL_TIMEOUT", "20")) + 5)
        return done.stdout.strip() if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""
    finally:
        _release(session_id, agent_id)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    try:
        if not _enabled():
            return 0
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if command == "stash":
            stash(payload)          # PreToolUse: prints nothing, so the dispatch is unaffected
        elif command == "start":
            out = start(payload)
            if out:
                print(out)
    except Exception:  # noqa: BLE001 — never block a dispatch or a subagent
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
