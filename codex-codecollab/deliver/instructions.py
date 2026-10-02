#!/usr/bin/env python3
"""Inject CodeCollab's static instructions once per session, and again after compaction.

    python3 instructions.py session-start      SessionStart hook (Claude Code, Codex)

stdin is the hook's JSON input; stdout is ``SessionStart`` ``additionalContext`` or nothing. The
texts (repository provenance, recall feedback, resolve guidance) are ``provenance.session_
instructions()``: the compact set by default, nothing in ``VONIC_CODECOLLAB_INSTRUCTIONS=legacy``
mode, which delivers the old texts on every prompt from ``recall.py`` instead.

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
import provenance  # noqa: E402

_SOURCES = frozenset({"startup", "clear", "compact"})


def session_start(event: dict) -> str | None:
    """The instruction text to inject for this SessionStart event, else None."""
    if os.environ.get("VONIC_CODECOLLAB_DISABLED") == "1" or capture.foreign_host():
        return None
    if event.get("source") not in _SOURCES:
        return None
    return provenance.session_instructions() or None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["session-start"]:
        return 0
    try:
        event = json.load(sys.stdin)
        text = session_start(event) if isinstance(event, dict) else None
        if text:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                                     "additionalContext": text}}))
    except Exception:  # noqa: BLE001 — never block or pollute a session start
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
