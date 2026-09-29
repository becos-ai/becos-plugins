#!/usr/bin/env python3
"""UserPromptSubmit recall for Codex: the vendored ``deliver/recall.py``, plus a visible notice.

Runs ``recall.py`` unchanged and forwards its output, adding a ``systemMessage`` so the user can see
what recall did on each prompt — Codex's UserPromptSubmit output schema accepts that field next to
``hookSpecificOutput``. The notice is shown to the user; the model's context is still only
``additionalContext``.

    codecollab: recalled 8,571 chars · acme/widget@main
    codecollab: no memory recalled
    codecollab: recall offline — session expired, say "log in"

Why a wrapper and not a change to ``recall.py``: that file is vendored byte-for-byte from the
Claude Code plugin, which also displays ``systemMessage``. Editing it would either fork it here or
put the notice on every Claude prompt too.

Fail-open, like every hook here: if anything goes wrong, recall's own output is forwarded untouched
(or nothing, if recall produced nothing). ``VONIC_CODECOLLAB_RECALL_NOTICE=0`` turns the notice off.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

RECALL = Path(__file__).resolve().parent.parent / "deliver" / "recall.py"

_MEMORY_RE = re.compile(r"<recalled-memory([^>]*)>\n(.*?)\n</recalled-memory>", re.S)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
_AUTH_EXPIRED = '<brain-recall-status kind="auth-expired">'


def _notice(context: str) -> str:
    """One line describing what this prompt's recall context contains."""
    if _AUTH_EXPIRED in context:
        return 'codecollab: recall offline — session expired, say "log in"'
    match = _MEMORY_RE.search(context)
    if not match:
        return "codecollab: no memory recalled"
    attrs = dict(_ATTR_RE.findall(match.group(1)))
    where = attrs.get("repo", "")
    if where and attrs.get("branch"):
        where += f"@{attrs['branch']}"
    return f"codecollab: recalled {len(match.group(2)):,} chars" + (f" · {where}" if where else "")


def _with_notice(raw: str) -> str:
    """Recall's stdout with a ``systemMessage`` added; ``raw`` unchanged if it isn't the expected
    single JSON object."""
    try:
        out = json.loads(raw)
        context = out["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        return raw
    if not isinstance(context, str):
        return raw
    out["systemMessage"] = _notice(context)
    return json.dumps(out)


def main() -> int:
    stdin = sys.stdin.read()
    try:
        done = subprocess.run([sys.executable, str(RECALL)], input=stdin,  # noqa: S603
                              capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 — never block the prompt
        return 0
    raw = done.stdout
    if raw.strip() and os.environ.get("VONIC_CODECOLLAB_RECALL_NOTICE", "1") != "0":
        try:
            raw = _with_notice(raw.strip()) + "\n"
        except Exception:  # noqa: BLE001 — a notice must never cost the recall itself
            pass
    sys.stdout.write(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
