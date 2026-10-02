#!/usr/bin/env python3
"""UserPromptSubmit handoff for Codex: the vendored ``deliver/handoff.py prompt-submit``, plus a
visible notice.

The startup handoff is the vendored Claude Code flow, unchanged: ``SessionStart`` runs
``handoff.py session-start`` (spawns a detached fetch, returns at once), and on each prompt
``handoff.py prompt-submit`` claims a ready result exactly once and returns it as
``additionalContext``. It rides the first prompt sent after the fetch finished, never one already
in flight. This wrapper forwards that output and adds a ``systemMessage`` on the one prompt that
carries it, so the user can see it arrive:

    codecollab: added handoff · 19 turns · acme/widget@main

Prompts that carry no handoff (most of them) produce no output and no notice. Same pattern and
reason as ``codex_recall.py``: ``handoff.py`` is vendored byte-for-byte, so the notice lives here.

Fail-open: if anything goes wrong, handoff.py's own output is forwarded untouched (or nothing).
``VONIC_CODECOLLAB_HANDOFF_NOTICE=0`` turns the notice off.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

HANDOFF = Path(__file__).resolve().parent.parent / "deliver" / "handoff.py"

# The first line of handoff.fetch()'s text: `session history — org/repo@branch` or
# `session history — org/repo (all branches)`, followed by the checkpoint as one JSON line.
_SCOPE_RE = re.compile(r"^session history — (.+)$", re.M)
_JSON_RE = re.compile(r"^(\{.*\})$", re.M)


def _notice(context: str) -> str:
    """One line describing the handoff this prompt carries."""
    parts = ["codecollab: added handoff"]
    match = _JSON_RE.search(context)
    if match:
        try:
            selection = json.loads(match.group(1))["checkpoint"]["selection"]
            turns = selection["included_turns"]
            if isinstance(turns, int) and not isinstance(turns, bool):
                parts.append(f"{turns} turn" + ("" if turns == 1 else "s"))
        except (ValueError, KeyError, TypeError):
            pass
    scope = _SCOPE_RE.search(context)
    if scope:
        parts.append(scope.group(1).strip())
    return " · ".join(parts)


def _with_notice(raw: str) -> str:
    """handoff.py's stdout with a ``systemMessage`` added; ``raw`` unchanged if it isn't the
    expected single JSON object."""
    try:
        out = json.loads(raw)
        context = out["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        return raw
    if not isinstance(context, str) or "<repository-handoff>" not in context:
        return raw
    out["systemMessage"] = _notice(context)
    return json.dumps(out)


def main() -> int:
    stdin = sys.stdin.read()
    try:
        done = subprocess.run([sys.executable, str(HANDOFF), "prompt-submit"], input=stdin,  # noqa: S603
                              capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 — never block the prompt
        return 0
    raw = done.stdout
    if raw.strip() and os.environ.get("VONIC_CODECOLLAB_HANDOFF_NOTICE", "1") != "0":
        try:
            raw = _with_notice(raw.strip()) + "\n"
        except Exception:  # noqa: BLE001 — a notice must never cost the handoff itself
            pass
    sys.stdout.write(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
