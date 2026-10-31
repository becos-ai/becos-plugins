#!/usr/bin/env python3
"""Structural session lifecycle facts (fork, revert), shared by every adapter.

A lifecycle fact is not dialogue: it records that a host forked or reverted a session, so memory can
link a fork to its parent without copying the parent's turns, and keep reverted work as history
instead of deleting it. The turn-only capture path cannot express that, so a fact rides the server's
existing ``other`` event with the fact in its free-form metadata bag.

Only strictly validated ids, enums and timestamps cross this boundary — never text, diffs, file
bodies or instructions. Arguments are persisted once under an id derived from the host's own source
event, so a retry never re-snapshots mutable repository identity. Accepted receipts are kept as
dedupe tombstones; a terminal rejection is parked, never rewritten as accepted.

First written for the OpenCode V2 adapter (``oc_lifecycle.py``); promoted here so Claude Code and
Codex record forks the same way. Tag ``oc`` keeps that adapter's id prefixes, event ids and
``other_type`` byte-for-byte, so facts it already delivered still deduplicate.

CLI: ``lifecycle.py append`` (fact JSON on stdin) | ``lifecycle.py retry --cwd=<dir>``.
"""
from __future__ import annotations

from datetime import datetime
import glob
import hashlib
import json
import os
import re
import sys
import time

import capture
import gbrain_client

# The host's name inside `other_type`; `opencode` is what the V2 adapter already sends.
_HOST = {"oc": "opencode", "cc": "claude_code", "cx": "codex", "cur": "cursor"}
_TYPES = ("fork", "revert_staged", "revert_cleared", "revert_committed")
_DESCRIPTIONS = {
    "fork": "Session fork created; inherited context is referenced, not copied.",
    "revert_staged": "Revert staged by the host; not a committed history rollback.",
    "revert_cleared": "Staged revert cleared by the host; target not included in this event.",
    "revert_committed": "Revert committed by the host; previously captured work remains historical memory.",
}
_GENERIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,239}")
_MAX_FACTS = 10_000


def _oc_id(value, prefix) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + r"[A-Za-z0-9_-]{1,240}", value) is not None


def _host_id(value) -> bool:
    return isinstance(value, str) and _GENERIC_ID.fullmatch(value) is not None


def project(payload):
    """Validate one fact. Returns (tag, session_id, source_event_id, cwd, iso_time, fact)."""
    if not isinstance(payload, dict):
        raise ValueError("object required")
    tag = payload.get("client_tag") or capture._client_tag()
    if tag not in _HOST:
        raise ValueError("unknown client tag")
    sid, source, cwd = payload.get("session_id"), payload.get("source_event_id", payload.get("event_id")), payload.get("cwd")
    # OpenCode ids carry typed prefixes the V2 adapter already enforced; other hosts use UUIDs.
    session_ok = _oc_id(sid, "ses_") if tag == "oc" else _host_id(sid)
    source_ok = _oc_id(source, "evt_") if tag == "oc" else _host_id(source)
    if not session_ok or not source_ok or not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError("invalid identity")
    stamp = payload.get("occurred_at")
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00")) if isinstance(stamp, str) and len(stamp) <= 40 else None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError("invalid timestamp")
    kind = payload.get("type")
    if kind not in _TYPES:
        raise ValueError("invalid lifecycle type")
    message_ok = (lambda v: _oc_id(v, "msg_")) if tag == "oc" else _host_id
    fact = {"session_id": sid, "source_event_id": source, "type": kind}
    if kind == "fork":
        parent = payload.get("parent_session_id")
        if not (_oc_id(parent, "ses_") if tag == "oc" else _host_id(parent)):
            raise ValueError("invalid fork parent")
        boundary = payload.get("boundary")
        # OpenCode always names the boundary; Claude Code and Codex may only know the parent.
        if boundary is not None or tag == "oc":
            if (not isinstance(boundary, dict) or boundary.get("type") not in ("before", "through")
                    or not message_ok(boundary.get("message_id"))):
                raise ValueError("invalid fork boundary")
        fact["parent_session_id"] = parent
        if boundary is not None:
            fact["boundary"] = {"type": boundary["type"], "message_id": boundary["message_id"]}
        fact["inherited_context"] = "reference_only"
    elif kind in ("revert_staged", "revert_committed"):
        if not message_ok(payload.get("target_message_id")):
            raise ValueError("invalid target")
        fact["target_message_id"] = payload["target_message_id"]
    return tag, sid, source, cwd, parsed.isoformat(), fact


def directory(tag: str | None = None) -> str:
    tag = tag or capture._client_tag()
    path = os.path.join(capture._sessions_dir(), f"{tag}-lifecycle")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def deliver(path, timeout=8):
    """Persist-first delivery of one fact; never raises, never exposes transport details."""
    with capture._locked(path):
        record = capture._load_buffer(path)
        if not record or record.get("uploaded") or record.get("parked"):
            return
        try:
            url, token = capture._resolve_becos()
            result = gbrain_client.call_tool(url, token, "vonic_remember", record["args"], timeout,
                                             extra_headers=capture._becos_identity_headers())
            receipt = gbrain_client.result_receipt(result, "vonic_remember")
            if receipt.present:
                accepted = (receipt.valid and receipt.acceptance == "accepted" and
                            (receipt.state == "pending" or (receipt.state == "terminal" and receipt.outcome == "succeeded")))
                if not accepted:
                    if receipt.valid and not receipt.retryable and not capture._receipt_auth(receipt, ""):
                        record["parked"] = True
                        capture._save_buffer(path, record)
                    return
            record["uploaded"] = True
            capture._save_buffer(path, record)
        except Exception:
            return


def append(payload, *, send: bool = True):
    tag, sid, source, cwd, stamp, fact = project(payload)
    digest = hashlib.sha256((sid + ":" + source).encode()).hexdigest()
    folder = directory(tag)
    path = os.path.join(folder, digest + ".json")
    with capture._locked(os.path.join(folder, "capacity")), capture._locked(path):
        existing = capture._load_buffer(path)
        if existing:
            if existing.get("fact") != fact or existing.get("cwd") != cwd or existing.get("occurred_at") != stamp:
                return {"status": "conflict"}
            duplicate = True
        else:
            if len(glob.glob(os.path.join(folder, "*.json"))) >= _MAX_FACTS:
                return {"status": "limit"}
            git = capture._git_identity(cwd, sid)
            if not capture._identity_scoped(git):
                return {"status": "unscoped"}   # never write lifecycle facts into the general brain
            buf = capture._new_buffer(sid, cwd, tag)
            metadata = capture._remember_metadata(buf, {"ts": stamp}, git["repo"], git["branch"], git["author"], git.get("commit"))
            event = metadata["event"]
            event.update(event_id=f"{tag}-lifecycle:" + digest, event_type="other",
                         other_type=f"{_HOST[tag]}_{fact['type']}")
            bag = event["metadata"]
            for key in ("turn_index", "event_id_version", "event_id_source", "source_message_id_count"):
                bag.pop(key, None)
            bag["lifecycle"] = fact
            args = {"mode": "fact", "content": _DESCRIPTIONS[fact["type"]] + "\n" + json.dumps(fact, sort_keys=True),
                    "metadata": metadata}
            capture._save_buffer(path, {"cwd": cwd, "occurred_at": stamp, "fact": fact, "args": args, "uploaded": False})
            duplicate = False
    if send:
        deliver(path)
    return {"status": "buffered", "appended": int(not duplicate), "duplicates": int(duplicate)}


def retry(cwd, limit=3, tag: str | None = None):
    """Deliver up to ``limit`` pending facts recorded for ``cwd`` (``None``: any directory)."""
    started = time.monotonic()
    count = 0
    for path in glob.glob(os.path.join(directory(tag), "*.json")):
        if count >= limit or time.monotonic() - started >= 20:
            break
        record = capture._load_buffer(path)
        if (cwd is None or record.get("cwd") == cwd) and not record.get("uploaded") and not record.get("parked"):
            deliver(path, timeout=5)
            count += 1
    return count


def main(argv: list[str]) -> int:
    if os.environ.get("VONIC_CODECOLLAB_DISABLED") == "1":
        return 0
    command = argv[0] if argv else ""
    try:
        if command == "append":
            print(json.dumps(append(json.load(sys.stdin))))
        elif command == "retry":
            cwd = next((a.split("=", 1)[1] for a in argv[1:] if a.startswith("--cwd=")), os.getcwd())
            print(json.dumps({"retried": retry(cwd)}))
        else:
            return 2
    except (ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "invalid"}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:  # noqa: BLE001 — a lifecycle fact must never break the session
        sys.exit(0)
