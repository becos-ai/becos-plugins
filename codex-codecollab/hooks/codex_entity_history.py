#!/usr/bin/env python3
"""PreToolUse(apply_patch) for Codex: the first edit of a file never shown its history is denied
once, with that history (vendored ``deliver/entity_history.py``; PLAN_entity_history.md).

Codex reads files through shell commands, so there is no read hook to carry history; the one-time
deny is Codex's only delivery path. The patch's ``*** Update File:`` / ``*** Delete File:`` paths
are looked up (``*** Add File:`` is new and has no history), at most ``MAX_FILES`` per patch so the
denial stays under Codex's default tool-feedback limit; files beyond that stay unshown and are
covered when the retried patch, or a later one, reaches them. The retry goes through for every file
already shown.

Fail-open: any error, timeout, or the feature being off (``VONIC_CODECOLLAB_ENTITY_HISTORY`` unset)
prints nothing and the patch applies normally.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deliver"))

MAX_FILES = 3
_PATCH_PATH = re.compile(r"^\*\*\* (?:Update|Delete) File: (.+?)\s*$", re.M)


def patch_paths(command: object) -> list[str]:
    if not isinstance(command, str):
        return []
    return list(dict.fromkeys(m.group(1) for m in _PATCH_PATH.finditer(command)))


def handle(payload: dict) -> dict | None:
    if payload.get("tool_name") != "apply_patch":
        return None
    tool_input = payload.get("tool_input") or {}
    paths = patch_paths(tool_input.get("command") if isinstance(tool_input, dict) else None)
    session = payload.get("session_id") or payload.get("thread_id")
    cwd = payload.get("cwd") or os.getcwd()
    if not paths or not isinstance(session, str):
        return None
    import entity_history
    block = entity_history.deliver("edit", paths, cwd, session, max_files=MAX_FILES)
    if not block:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"{block}\n\n{entity_history.RETRY_LINE}",
    }}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        out = handle(payload) if isinstance(payload, dict) else None
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception:  # noqa: BLE001 — a hook must never block a patch on our failure
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
