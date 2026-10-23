#!/usr/bin/env python3
"""UserPromptSubmit handoff for Codex: runs the vendored ``deliver/handoff.py prompt-submit`` and
forwards its output.

The startup handoff is the vendored Claude Code flow, unchanged: ``SessionStart`` runs
``handoff.py session-start`` (spawns a detached fetch, returns at once), and on each prompt
``handoff.py prompt-submit`` claims a ready result exactly once and returns it as
``additionalContext``, with the user-only notice

    codecollab: added handoff · 19 turns · acme/widget@main

which ``handoff.py`` now adds itself (core 0.40.0, ``VONIC_CODECOLLAB_HANDOFF_NOTICE=0`` turns it
off). This wrapper stays as the hook entry point so ``hooks.json`` and Codex's trust record for it
do not change. Fail-open: on any error nothing is printed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HANDOFF = Path(__file__).resolve().parent.parent / "deliver" / "handoff.py"


def main() -> int:
    stdin = sys.stdin.read()
    try:
        done = subprocess.run([sys.executable, str(HANDOFF), "prompt-submit"], input=stdin,  # noqa: S603
                              capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 — never block the prompt
        return 0
    sys.stdout.write(done.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
