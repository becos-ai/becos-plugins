#!/usr/bin/env python3
"""Inject CodeCollab's static instructions once per session, and again after compaction.

    python3 instructions.py session-start      SessionStart hook (Claude Code, Codex)

stdin is the hook's JSON input; stdout is ``SessionStart`` ``additionalContext`` (and, with projects
on, a ``systemMessage`` notice for the user) or nothing. The
texts (repository provenance, recall feedback, resolve guidance) are ``provenance.session_
instructions()``: the compact set by default, nothing in ``VONIC_CODECOLLAB_INSTRUCTIONS=legacy``
mode, which delivers the old texts on every prompt from ``recall.py`` instead. With
``VONIC_CODECOLLAB_RULEBOOK=1`` the user's accepted rules (``rulebook.py``, cached) follow, in both
modes, and a detached child refreshes that cache. With ``VONIC_CODECOLLAB_PROJECTS=1`` the
session's project is resolved locally (``project.py``: git repo, else ``.becos-project``): the user
is told which project the conversation is saved to, and the agent is told it too — or, when the
folder has none yet, told to ask the user and record the answer.

Sources:
  startup, clear   a fresh context: inject.
  compact          compaction summarised the earlier injection away: inject again. Claude Code and
                   Codex both run ``compact`` hooks before the next model request, mid-turn included.
  resume, fork     the transcript already carries the earlier injection: inject nothing.

Runs under OpenCode's Claude bridge too (unlike capture and handoff, which stand down there): the
bridge drops OpenCode's system prompt, so this hook is the only way the texts reach Claude. Stands
down when another host such as Cursor runs these Claude Code hooks (``capture.foreign_host``); that
host's own plugin delivers them. Local only, no network: Claude Code holds the first response until
SessionStart hooks finish. Fail-open: any error prints nothing.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402
import project  # noqa: E402
import provenance  # noqa: E402
import rulebook  # noqa: E402

_SOURCES = frozenset({"startup", "clear", "compact"})


def project_start(event: dict) -> tuple[str, str]:
    """``(context for the agent, notice for the user)`` for the session's project; ``("", "")``
    when projects are off. Local only: the project list is refreshed in a detached child."""
    if not project.enabled():
        return "", ""
    cwd = event.get("cwd") or os.getcwd()
    session_id = event.get("session_id")
    resolved = project.start(cwd, session_id)
    project.spawn_refresh(cwd)
    bridged = capture.hosted_by_opencode()     # OpenCode's plugin asks and shows the notice
    notice = project.message(resolved) if event.get("source") != "compact" and not bridged else ""
    if resolved is None:
        return ("" if bridged else project.ask_instructions(cwd, session_id)), notice
    if resolved.source == "git":
        context = (f"CodeCollab project for this session: {resolved.name} (git repo; it can't be "
                   "changed).")
    else:
        context = (f"CodeCollab project for this session: {resolved.name} ({resolved.type}). If "
                   "the user asks to use another project for this session, run "
                   f'`python3 "{project.__file__}" --cwd "{cwd}" --session "{session_id}" list` '
                   "and then `... use <name>` (or `use --new [<name>]`).")
    return context, notice


def session_start(event: dict) -> str | None:
    """The instruction text to inject for this SessionStart event, else None."""
    text, _notice = session_start_output(event)
    return text


def session_start_output(event: dict) -> tuple[str | None, str | None]:
    """(``additionalContext``, ``systemMessage``) for this SessionStart event."""
    if os.environ.get("VONIC_CODECOLLAB_DISABLED") == "1" or capture.foreign_host():
        return None, None
    if event.get("source") not in _SOURCES:
        return None, None
    text = provenance.session_instructions() or ""
    if rulebook.enabled():
        # The user's accepted rules from the local cache (local only — this hook must stay fast),
        # then a detached refresh for next time. No cache yet: the first prompt injects the fresh
        # section once (recall.py). Delivered in both instruction modes.
        cwd = event.get("cwd") or os.getcwd()
        section = rulebook.session_section(cwd)
        rulebook.spawn_refresh(cwd, event.get("session_id"),
                               pending=not section and event.get("source") != "compact")
        if section:
            text = f"{text}\n\n{section}" if text else section
    try:
        context, notice = project_start(event)
    except Exception:  # noqa: BLE001 — the project step never blocks the rest
        context, notice = "", ""
    if context:
        text = f"{text}\n\n{context}" if text else context
    return text or None, notice or None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["session-start"]:
        return 0
    try:
        event = json.load(sys.stdin)
        text, notice = session_start_output(event) if isinstance(event, dict) else (None, None)
        out: dict = {}
        if text:
            out["hookSpecificOutput"] = {"hookEventName": "SessionStart",
                                         "additionalContext": text}
        if notice:
            out["systemMessage"] = notice
        if out:
            print(json.dumps(out))
    except Exception:  # noqa: BLE001 — never block or pollute a session start
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
