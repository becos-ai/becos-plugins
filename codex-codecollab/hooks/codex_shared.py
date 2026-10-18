#!/usr/bin/env python3
"""UserPromptSubmit shared-handoff notice for Codex: the vendored ``deliver/shared_handoff.py
notify``, plus a visible line.

``notify`` claims the inbox's newly addressed packages exactly once and returns them as
``additionalContext`` (a ``<shared-handoff-notice>`` block that names the ``codecollab-shared``
skill, since this hook runs as tag ``cx``), then starts a background inbox refresh for the next
prompt. This wrapper forwards that output and adds a ``systemMessage`` on the one prompt that
carries it, so the user sees it arrive too:

    codecollab: 2 new team handoffs — ask to see your CodeCollab inbox

Same pattern as ``codex_handoff.py``: ``shared_handoff.py`` is vendored byte-for-byte, so the
notice lives here. Fail-open: on any error the core's output is forwarded untouched (or nothing).
``VONIC_CODECOLLAB_SHARED_NOTICE=0`` turns the visible line off; the context block is unaffected.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parent.parent / "deliver" / "shared_handoff.py"


def _with_notice(raw: str) -> str:
    """``notify``'s stdout with a ``systemMessage`` added; ``raw`` unchanged if it isn't the
    expected single JSON object carrying a notice."""
    try:
        out = json.loads(raw)
        context = out["hookSpecificOutput"]["additionalContext"]
    except (ValueError, KeyError, TypeError):
        return raw
    if not isinstance(context, str) or "<shared-handoff-notice>" not in context:
        return raw
    count = sum(1 for line in context.splitlines() if line.startswith("- "))
    noun = "handoff" if count == 1 else "handoffs"
    out["systemMessage"] = f"codecollab: {count} new team {noun} — ask to see your CodeCollab inbox"
    return json.dumps(out)


def main() -> int:
    if os.environ.get("VONIC_CODECOLLAB_SHARED_HANDOFFS", "1") != "1":
        return 0
    stdin = sys.stdin.read()
    try:
        done = subprocess.run([sys.executable, str(SHARED), "notify"], input=stdin,  # noqa: S603
                              capture_output=True, text=True, check=False, timeout=4)
    except Exception:  # noqa: BLE001 — never block the prompt
        return 0
    raw = done.stdout
    if raw.strip() and os.environ.get("VONIC_CODECOLLAB_SHARED_NOTICE", "1") != "0":
        try:
            raw = _with_notice(raw.strip()) + "\n"
        except Exception:  # noqa: BLE001 — a visible line must never cost the notice itself
            pass
    sys.stdout.write(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
