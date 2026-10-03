#!/usr/bin/env python3
"""File/symbol history for coding agents (PLAN_entity_history.md, vonic_stack docs).

When the agent first reads a file in a session, the file's recorded history (decisions,
constraints, rejected approaches, superseded states) is added to the read result. When it first
edits a file it has NOT been shown, the edit is blocked once and the block message carries the
history; the retry goes through. Each file gets its history at most once per session.

The history comes from ``vonic_query`` with sibling metadata ``recall_kind="entity_history"``,
``entities=[{path}]`` and ``budget_chars``; becos_memforest answers from its entity trees.

Everything fails open: a timeout, an error, an empty history or the feature being off never blocks
or delays a tool beyond the lookup timeout. Off by default (``VONIC_CODECOLLAB_ENTITY_HISTORY=1``).

Claude Code hook usage (stdin = the hook's JSON payload)::

  python3 entity_history.py post-read   # PostToolUse(Read)        -> additionalContext
  python3 entity_history.py pre-edit    # PreToolUse(Edit|Write|…) -> one-time deny with history

Adapters (Cursor, Codex, OpenCode) call :func:`deliver` directly, or ``entity_history.py json``
with ``{"mode": "read"|"edit", "paths": [...], "cwd": ..., "session_id": ..., "max_files"?: N}`` on
stdin, which prints ``{"text": <block or "">, "retry": RETRY_LINE}``.

Settings: ``VONIC_CODECOLLAB_ENTITY_HISTORY`` (off), ``VONIC_CODECOLLAB_ENTITY_HISTORY_CHARS``
(2,500 per file, 1,000-4,000), ``VONIC_ENTITY_HISTORY_TIMEOUT`` (8 s).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import capture  # noqa: E402
import gbrain_client  # noqa: E402
import recall  # noqa: E402

DEFAULT_CHARS = 2_500
MIN_CHARS, MAX_CHARS = 1_000, 4_000
DEFAULT_TIMEOUT = 8.0
MAX_ATTEMPTS = 2            # an erroring lookup is retried once on a later read/edit
_PENDING_STALE_S = 60.0
_STATE_MAX_AGE_S = 7 * 86_400
# Opening of vonic_agent's ENTITY_HISTORY_HEADER (api/memory_routing.py); pinned by tests on both
# sides. Matched as a header so an answer that is ONLY the header still counts as empty.
ENTITY_HISTORY_HEADER_OPENING = "recalled from captured coding sessions"
RETRY_LINE = ("CodeCollab showed this file's recorded history once instead of applying the edit. "
              "Take it into account, then retry the edit; it will not be blocked again.")
_SYMBOL_HEADER = re.compile(r"^### symbol (?P<name>.+)$")
_SECTION_END = re.compile(r"^(\[\d+ older entries omitted\]|History of .+)$")


# ── settings ─────────────────────────────────────────────────────────────────

def enabled() -> bool:
    env = os.environ
    if env.get("VONIC_CODECOLLAB_DISABLED") == "1":
        return False
    if env.get("VONIC_CODECOLLAB_ENTITY_HISTORY", "0").strip() != "1":
        return False
    # Same stand-downs as the other Claude Code hooks (Cursor imports them; OpenCode's bridge).
    return not (capture.foreign_host() or capture.hosted_by_opencode())


def budget_chars() -> int:
    try:
        value = int(os.environ.get("VONIC_CODECOLLAB_ENTITY_HISTORY_CHARS", "").strip()
                    or DEFAULT_CHARS)
    except ValueError:
        return DEFAULT_CHARS
    return min(max(value, MIN_CHARS), MAX_CHARS)


def timeout_s() -> float:
    return recall._parse_positive_float(os.environ.get("VONIC_ENTITY_HISTORY_TIMEOUT"),
                                        DEFAULT_TIMEOUT)


def _log(line: str) -> None:
    if os.environ.get("VONIC_CODECOLLAB_DEBUG") != "1":
        return
    try:
        with open(os.path.join(capture._cache_dir(), "entity-history.log"), "a",
                  encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')}  {capture._client_tag()}  {line}\n")
    except OSError:
        pass


# ── paths ────────────────────────────────────────────────────────────────────

def _toplevel(cwd: str) -> str | None:
    return capture._git(cwd, "rev-parse", "--show-toplevel") or None


def normalize_paths(raw_paths, cwd: str) -> list[str]:
    """Repository-relative paths inside ``cwd``'s work tree; outside and sensitive paths dropped.

    Not ``capture._repo_relative_files``: that returns nothing when ``VONIC_CAPTURE_FILES=0`` (a
    capture switch), and reading history must not depend on what this machine uploads."""
    root = _toplevel(cwd)
    if not root:
        return []
    root = os.path.realpath(root)
    out: list[str] = []
    for path in raw_paths or []:
        if not isinstance(path, str) or not path.strip():
            continue
        absolute = path if os.path.isabs(path) else os.path.join(cwd, path)
        rel = os.path.relpath(os.path.realpath(os.path.normpath(absolute)), root)
        if rel.startswith("..") or os.path.isabs(rel) or rel == ".":
            continue
        rel = rel.replace(os.sep, "/")
        if capture._secret_path(rel) or rel in out:
            continue
        out.append(rel)
    return out


# ── per-session "already shown" state ────────────────────────────────────────

def _state_dir() -> str:
    path = os.path.join(capture._cache_dir(), capture._client_tag(), "entity-history")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _state_path(session_id: str) -> str | None:
    slug = capture._slugify(session_id or "")
    return os.path.join(_state_dir(), f"{slug}.json") if slug else None


def _read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: str, state: dict) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, path)


def sweep(now: float | None = None) -> None:
    """Drop session state older than a week (best-effort)."""
    now = now or time.time()
    try:
        directory = _state_dir()
        for name in os.listdir(directory):
            full = os.path.join(directory, name)
            if now - os.path.getmtime(full) > _STATE_MAX_AGE_S:
                os.remove(full)
    except OSError:
        pass


def claim(session_id: str, paths: list[str], limit: int | None = None) -> list[str]:
    """Atomically mark which of ``paths`` this caller will look up now.

    A path is skipped when its history was already shown (``delivered``), known empty, being looked
    up by a concurrent hook (``pending``, unless stale), or has failed ``MAX_ATTEMPTS`` times."""
    state_path = _state_path(session_id)
    if not state_path or not paths:
        return []
    now = time.time()
    with capture._locked(state_path):
        state = _read_state(state_path)
        claimed = []
        for path in paths:
            if limit is not None and len(claimed) >= limit:
                break
            entry = state.get(path) or {}
            status = entry.get("status")
            if status in ("delivered", "empty"):
                continue
            if status == "pending" and now - float(entry.get("at") or 0) < _PENDING_STALE_S:
                continue
            if status == "error" and int(entry.get("attempts") or 0) >= MAX_ATTEMPTS:
                continue
            state[path] = {"status": "pending", "at": now,
                           "attempts": int(entry.get("attempts") or 0)}
            claimed.append(path)
        if claimed:
            _write_state(state_path, state)
    return claimed


def settle(session_id: str, outcomes: dict[str, str]) -> None:
    """Record each claimed path's outcome: ``delivered``, ``empty`` or ``error``."""
    state_path = _state_path(session_id)
    if not state_path or not outcomes:
        return
    with capture._locked(state_path):
        state = _read_state(state_path)
        for path, status in outcomes.items():
            entry = state.get(path) or {}
            attempts = int(entry.get("attempts") or 0) + (1 if status == "error" else 0)
            state[path] = {"status": status, "at": time.time(), "attempts": attempts}
        _write_state(state_path, state)


def mark_delivered(session_id: str, paths: list[str]) -> None:
    """Record ``paths`` as already shown (e.g. prompt recall carried their history)."""
    paths = [p for p in dict.fromkeys(paths) if isinstance(p, str) and p]
    state_path = _state_path(session_id)
    if not state_path or not paths:
        return
    with capture._locked(state_path):
        state = _read_state(state_path)
        for path in paths:
            if (state.get(path) or {}).get("status") != "delivered":
                state[path] = {"status": "delivered", "at": time.time(), "attempts": 0}
        _write_state(state_path, state)


def release(session_id: str, paths: list[str]) -> None:
    """Return claimed paths to unclaimed (the history was found but could not be delivered)."""
    state_path = _state_path(session_id)
    if not state_path or not paths:
        return
    with capture._locked(state_path):
        state = _read_state(state_path)
        for path in paths:
            entry = state.get(path) or {}
            if entry.get("status") == "pending":
                state.pop(path, None)
        _write_state(state_path, state)


# ── lookup ───────────────────────────────────────────────────────────────────

def _is_empty(answer: str) -> bool:
    text = answer.strip().lower()
    if text.startswith(ENTITY_HISTORY_HEADER_OPENING):
        _, sep, rest = text.partition("\n\n")
        text = rest.strip() if sep else ""
    return len(text) < 12 or any(s in text for s in recall._SERVER_SENTINELS)


def fetch(path: str, cwd: str, session_id: str | None) -> str | None:
    """One file's history text, ``""`` when there is none. Raises on transport/auth failure."""
    scope = recall._query_scope(cwd, session_id)
    if scope is None:
        return ""
    scope = {**scope, "recall_kind": "entity_history", "entities": [{"path": path}],
             "budget_chars": budget_chars()}
    url, token = capture._resolve_becos()
    result = gbrain_client.call_tool(
        url, token, "vonic_query", {"prompt": f"(history of {path})", "metadata": scope},
        timeout_s(), extra_headers=capture._becos_identity_headers(),
    )
    receipt = gbrain_client.result_receipt(result, "vonic_query")
    if recall._receipt_auth(receipt):
        raise gbrain_client.GbrainError(f"structured auth rejection: {receipt.condition}")
    if receipt.present and not (receipt.valid and receipt.acceptance == "accepted"
                                and receipt.state == "terminal"
                                and receipt.outcome == "answered"):
        return ""
    answer = recall._answer_text(result)
    return "" if _is_empty(answer) else answer


def filter_symbols(text: str, path: str, cwd: str) -> str:
    """Drop ``### symbol X`` sections whose name does not occur in the file's current text.

    Some extracted ``file::symbol`` keys are misattributed (a constant from another repo filed under
    this file); a symbol absent from the file is not this file's history. Unreadable file: keep."""
    try:
        with open(os.path.join(_toplevel(cwd) or cwd, path), encoding="utf-8",
                  errors="replace") as fh:
            source = fh.read()
    except OSError:
        return text
    out: list[str] = []
    dropping = False
    for line in text.splitlines():
        header = _SYMBOL_HEADER.match(line)
        if header:
            name = header.group("name").strip()
            dropping = name.rsplit(".", 1)[-1] not in source
            if not dropping:
                out.append(line)
            continue
        if dropping and _SECTION_END.match(line):
            dropping = False
        if not dropping:
            out.append(line)
    return "\n".join(out)


def wrap(text: str, paths: list[str], cwd: str) -> str:
    repo = capture._repo_slug(cwd) or ""
    attrs = ' source="codecollab"'
    if repo:
        attrs += f' repo="{recall._xml_attr(repo)}"'
    attrs += f' path="{recall._xml_attr(",".join(paths))}"'
    return f"<entity-history{attrs}>\n{text.strip()}\n</entity-history>"


def deliver(mode: str, raw_paths, cwd: str, session_id: str | None, *,
            max_files: int | None = None) -> str:
    """The wrapped history block for the files of one read/edit, or ``""``.

    ``mode`` is ``read`` or ``edit``; edits skip files that do not exist yet (a new file has no
    history). Each file is claimed before its lookup and settled after it, so every file is shown at
    most once per session across reads, edits and concurrent hooks. ``max_files`` caps how many
    not-yet-shown files one call looks up (the rest stay unshown for a later call)."""
    if not enabled() or not session_id or not cwd:
        return ""
    paths = normalize_paths(raw_paths, cwd)
    if mode == "edit":
        root = _toplevel(cwd) or cwd
        paths = [p for p in paths if os.path.isfile(os.path.join(root, p))]
    claimed = claim(session_id, paths, max_files)
    if not claimed:
        return ""
    deadline = time.monotonic() + timeout_s()
    texts: list[str] = []
    shown: list[str] = []
    outcomes: dict[str, str] = {}
    for path in claimed:
        if time.monotonic() >= deadline:
            break                              # unclaimed files are retried on a later tool call
        started = time.monotonic()
        try:
            text = fetch(path, cwd, session_id)
        except Exception as exc:  # noqa: BLE001 — fail open, never block a tool on our failure
            outcomes[path] = "error"
            _log(f"{mode:<5} error  {path}  {type(exc).__name__}: {str(exc)[:160]}")
            continue
        text = filter_symbols(text, path, cwd) if text else ""
        outcomes[path] = "delivered" if text else "empty"
        _log(f"{mode:<5} {outcomes[path]:<9} {path}  {len(text)}c  "
             f"{(time.monotonic() - started):.2f}s")
        if text:
            texts.append(text)
            shown.append(path)
    settle(session_id, outcomes)
    release(session_id, [p for p in claimed if p not in outcomes])
    if not texts:
        return ""
    return wrap("\n\n".join(texts), shown, cwd)


# ── Claude Code hook entry points ────────────────────────────────────────────

def _hook_paths(payload: dict, tools: set[str]) -> list[str]:
    name = payload.get("tool_name")
    if name not in tools:
        return []
    return capture._tool_paths(name, payload.get("tool_input") or {}, payload.get("cwd"))


def post_read(payload: dict) -> dict | None:
    paths = _hook_paths(payload, capture._READ_TOOLS)
    block = deliver("read", paths, payload.get("cwd") or os.getcwd(), payload.get("session_id"))
    if not block:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": block}}


def pre_edit(payload: dict) -> dict | None:
    paths = _hook_paths(payload, capture._EDIT_TOOLS)
    block = deliver("edit", paths, payload.get("cwd") or os.getcwd(), payload.get("session_id"))
    if not block:
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"{block}\n\n{RETRY_LINE}",
    }}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        if command == "json":
            max_files = payload.get("max_files")
            text = deliver(str(payload.get("mode") or "read"), payload.get("paths") or [],
                           str(payload.get("cwd") or os.getcwd()), payload.get("session_id"),
                           max_files=max_files if isinstance(max_files, int) else None)
            sys.stdout.write(json.dumps({"text": text, "retry": RETRY_LINE}))
            return 0
        handler = {"post-read": post_read, "pre-edit": pre_edit}.get(command)
        if handler is None:
            return 0
        if command == "post-read":
            sweep()
        out = handler(payload)
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception as exc:  # noqa: BLE001 — a hook must never break the session
        _log(f"unexpected {type(exc).__name__}: {str(exc)[:200]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
