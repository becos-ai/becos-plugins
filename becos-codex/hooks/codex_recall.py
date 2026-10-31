#!/usr/bin/env python3
"""UserPromptSubmit recall for Codex: runs the vendored ``deliver/recall.py`` and forwards its output.

The user-only ``systemMessage`` notice this wrapper used to add —

    becos: recalled 8,571 chars · acme/widget@main
    becos: no memory recalled
    becos: recall offline — session expired, say "log in"

— is now produced by ``recall.py`` itself for every runtime whose host renders it (Claude Code and
Codex), worded identically, and only when a lookup actually ran (core 0.40.0). So this wrapper
forwards output untouched; it stays as the hook entry point so ``hooks.json`` and Codex's trust
record for it do not change. ``VONIC_CODECOLLAB_RECALL_NOTICE=0`` still turns the notice off.

Fail-open: if anything goes wrong, nothing is printed and the prompt proceeds.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RECALL = Path(__file__).resolve().parent.parent / "deliver" / "recall.py"


def _recall_env(stdin: str) -> dict:
    """The environment for recall.py: how this session was started (``exec`` for ``codex exec``,
    learnt by the capture hook from the rollout), so a headless job's heartbeat-style prompt gets no
    recall (TODOS #92). Unknown until the session's first turn is captured."""
    env = dict(os.environ)
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import codex_capture  # noqa: PLC0415 — the capture hook's per-session state
        session_id = str(json.loads(stdin).get("session_id") or "")
        source = codex_capture._load_pending(session_id).get("source") if session_id else ""
        if isinstance(source, str) and source:
            env["VONIC_CODECOLLAB_ENTRYPOINT"] = source
    except Exception:  # noqa: BLE001 — never block the prompt
        pass
    return env


def main() -> int:
    stdin = sys.stdin.read()
    try:
        done = subprocess.run([sys.executable, str(RECALL)], input=stdin,  # noqa: S603
                              capture_output=True, text=True, check=False,
                              env=_recall_env(stdin))
    except Exception:  # noqa: BLE001 — never block the prompt
        return 0
    sys.stdout.write(done.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
