#!/usr/bin/env python3
"""Capture Claude Code turns incrementally and ship them to the tenant's gbrain.

Per-turn, not per-session — so nothing waits for a session that may run for hours
or never cleanly end, and each upload is one small turn, never one huge blob.

Hooks (see ../hooks/hooks.json):
  * `capture.py turn`      — Stop: record the just-finished turn(s), ship them.
  * `capture.py sweep`     — SessionStart: retry any turns a prior session left pending.
  * `capture.py finalize`  — SessionEnd: flush the tail; mark the session done.
  * `capture.py --deliver <buffer>` — internal, detached: write the session so far
                             into one page body via put_page (overwrite by slug).
  * `capture.py deliver-session [turn|finalize]` — non-Claude-Code adapters pipe a
                             pre-normalized session (see _record_session) on stdin; same
                             buffer + delivery path. Used by ports, e.g. Opencode (client-tag oc).

PRIVACY CONTRACT
  Only the user's prompts and Claude's *text* replies leave this machine. Thinking
  and tool_use blocks (the code and patches) are never read; only the file *paths*
  touched are recorded. Fenced code in prose is stripped by default.

Each turn is buffered locally first (crash-resilient), then delivered detached. A
lock serialises deliveries so a turn is never uploaded twice.

Three delivery transports, in precedence order:
  * becos (MCP, default): the hosted gateway, or VONIC_BECOS_URL / VONIC_GATEWAY_URL to
    override. One `vonic_remember(mode="fact")` call per turn, carrying `metadata.event` —
    the repository identity (repo, branch, author, commit, changed files) that puts the turn
    in repository memory. See _remember_metadata and _resolve_gateway.
  * REST (custom endpoint): set VONIC_LOG_URL + VONIC_LOG_TOKEN. One JSON POST per
    message ({timestamp, role, text, repositoryName?, branch?, commitId?}, plus
    {sessionId, turnIndex} unless VONIC_LOG_LINK_FIELDS=0) with a stable
    Idempotency-Key. The payload contract is frozen in
    contracts/coding-agent-message.json — read it before adding a key.
  * gbrain (MCP, fallback): VONIC_GBRAIN_URL + VONIC_GBRAIN_TOKEN direct, or
    VONIC_GATEWAY_URL (+ token via connect.py) for discovery. Whole session as one page body.
"""

from __future__ import annotations

import fcntl
import glob
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gbrain_client  # noqa: E402
import rest_client  # noqa: E402

_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INDENT_CODE_RE = re.compile(r"(?m)^(?: {4}|\t).*(?:\n|$)")
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# capturing variant of _FENCE_RE (which only strips) — keeps language + body for opt-in code capture
_FENCE_CAPTURE_RE = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL)
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "Update", "Create"}
_CONFIG_TTL = 3600  # fallback cache lifetime when the gateway gives no expiry
# Hosted gateway. Last resort in _resolve_gateway, so a downloaded plugin works after
# /codecollab:login alone; self-hosted installs override with VONIC_GATEWAY_URL.
_DEFAULT_GATEWAY = "https://becos.ai"
_LOGIN_COMMAND_RE = re.compile(
    r"(?:^\s*/codecollab:login(?:\s|$)|<command-(?:message|name)>\s*/?codecollab:login\s*</command-(?:message|name)>)",
    re.IGNORECASE,
)


# ── small utils ──────────────────────────────────────────────────────────────

_WARNED: set[str] = set()


def _warn(msg: str) -> None:
    """Print once per process, regardless of VONIC_CODECOLLAB_DEBUG.

    Reserved for events the user must not miss: a rejected gateway URL, a gateway
    overridden away from the one they logged in to, a token withheld from a foreign host.
    Silent redirection is what makes gateway poisoning dangerous, so these are never
    debug-gated — but they are deduped so a busy session can't be spammed.
    """
    if msg in _WARNED:
        return
    _WARNED.add(msg)
    sys.stderr.write(f"[codecollab] WARNING: {msg}\n")


def _debug(msg: str) -> None:
    if os.environ.get("VONIC_CODECOLLAB_DEBUG") == "1":
        sys.stderr.write(f"[codecollab] {msg}\n")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "codecollab")
    os.makedirs(path, exist_ok=True)
    return path


def _client_tag() -> str:
    """This runtime's tag (cc/cx/oc). It names the per-runtime buffer namespace so a plugin only
    ever sweeps/delivers ITS OWN buffers. A shared queue let one runtime deliver another's buffer to
    its own VONIC_BECOS_URL / identity (mis-routing), since a buffer carries content but not its
    destination. Each runtime MUST set VONIC_CODECOLLAB_CLIENT_TAG (codex=cx, oc=oc); cc is the
    Claude default. See docs/DECISIONS.md."""
    return os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc")


def _sessions_dir() -> str:
    # Namespaced per runtime: <cache>/codecollab/<tag>/sessions/. buffer/lock paths and the sweep
    # all derive from here, so they follow automatically.
    path = os.path.join(_cache_dir(), _client_tag(), "sessions")
    os.makedirs(path, exist_ok=True)
    return path


def _migrate_legacy_buffers() -> None:
    """One-time relocation of pre-namespacing buffers from the shared `<cache>/sessions/` into their
    per-runtime dir, keyed by each buffer's OWN `client_tag`. Best-effort and idempotent; runs at
    SessionStart until the legacy dir drains. It MOVES (never delivers), so a `cc` buffer can't be
    shipped by an `oc` sweep — it just lands in `<cache>/cc/sessions/` for Claude to handle. rename
    keeps the inode, so a lock held by an in-flight deliverer is not broken (unlike unlink)."""
    legacy = os.path.join(_cache_dir(), "sessions")
    if not os.path.isdir(legacy):
        return
    for src in glob.glob(os.path.join(legacy, "*.json")):
        try:
            with open(src, encoding="utf-8") as fh:
                tag = json.load(fh).get("client_tag") or "cc"
        except (OSError, ValueError):
            tag = "cc"
        dest_dir = os.path.join(_cache_dir(), tag, "sessions")
        os.makedirs(dest_dir, exist_ok=True)
        base = os.path.basename(src)
        for suffix in ("", ".lock"):
            s, d = src + suffix, os.path.join(dest_dir, base + suffix)
            if os.path.exists(s) and not os.path.exists(d):
                try:
                    os.rename(s, d)
                except OSError:
                    pass
    try:
        if not os.listdir(legacy):
            os.rmdir(legacy)
    except OSError:
        pass


def _state_path() -> str:
    return os.path.join(_cache_dir(), "state.json")


def _load_state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        with open(_state_path(), "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except OSError:
        pass


_PROJECTS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def _count_historical_sessions() -> int:
    """Fast count of past session transcripts on this machine (no parsing — keep
    SessionStart snappy; exact turn counts come later from backfill.py's dry run)."""
    try:
        return len(glob.glob(os.path.join(_PROJECTS_DIR, "*", "*.jsonl")))
    except OSError:
        return 0


def _first_run_notice() -> None:
    """Once per machine, let Claude know historical sessions can be backfilled.

    Hooks run without a terminal and cannot prompt, so we inject SessionStart context
    (`additionalContext`) and let Claude offer `/codecollab:backfill` in chat — the one
    place the user can actually see a dry-run and confirm. Fires exactly once (guarded by
    a marker in state.json); never auto-delivers, so no fresh install kicks off a burst of
    distillation on its own. Disable with VONIC_CODECOLLAB_BACKFILL_NOTICE=0."""
    if os.environ.get("VONIC_CODECOLLAB_BACKFILL_NOTICE", "1") == "0":
        return
    state = _load_state()
    if state.get("backfill_notified"):
        return
    n = _count_historical_sessions()
    state["backfill_notified"] = True
    state["backfill_notified_at"] = _iso_now()
    _save_state(state)
    if n <= 0:
        return
    msg = (
        f"codecollab was just set up, and {n} earlier Claude Code session transcript(s) on this "
        "machine predate memory capture — they are not in the backend yet. Near the start of your "
        "next reply, briefly let the user know they can ingest that history by running the "
        "`/codecollab:backfill` command (it shows a dry run of per-project session and turn counts "
        "first, and only delivers after they confirm), then continue with whatever they asked. "
        "Mention this exactly once; do not bring it up again in later turns."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": msg,
        }
    }))


def _strip_code(text: str) -> str:
    if os.environ.get("VONIC_CODECOLLAB_STRIP_CODE", "1") == "0":
        return text
    return _FENCE_RE.sub("[code omitted]", text)


def _clean(text: str) -> str:
    text = _strip_code(text.strip())
    cap = _env_int("VONIC_CODECOLLAB_MAX_CHARS", 100_000)
    return text if cap <= 0 or len(text) <= cap else text[:cap] + "\n[truncated]"


def _slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
        return (out.stdout.strip() or None) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"


def _log_delivery(session_id: str, turn, role: str, idem: str, status: str) -> None:
    """Append a durable record of each delivery (survives buffer deletion). The
    Idempotency-Key is the join key to look a message up in the destination store.

    Both transports write here. On becos ``role`` is ``turn`` and ``idem`` is the event id
    (``session:seq``), which is that path's idempotency key. Delivery runs detached with stderr on
    DEVNULL, so `_debug` reaches nobody — this file is the only account of what left the machine,
    and its absence is why a stalled queue went unnoticed.
    """
    try:
        with open(os.path.join(_cache_dir(), "delivery.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{_iso_now()}  session={session_id}  turn={turn}  role={role}  "
                     f"idem={idem}  status={status}\n")
    except OSError:
        pass


def _log_dead_letter(session_id: str, turn: dict, error: str) -> bool:
    """Park a turn that has exhausted its delivery attempts, so giving up is never silent loss.

    One JSON object per line (greppable *and* machine-readable): the turn's text is preserved, so a
    server-side fix can be followed by a manual re-drive instead of the turn being gone. Written
    before the turn is marked uploaded — the mark is what stops the re-sends, this is what makes
    stopping safe. Local only and not rotated. Failure is returned to the caller so the turn remains
    pending rather than being silently discarded.
    """
    try:
        with open(os.path.join(_cache_dir(), "dead-letter.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": _iso_now(),
                "session_id": session_id,
                "seq": turn.get("seq", 0),
                "attempts": turn.get("attempts", 0),
                "error": error[:500],
                "ts": turn.get("ts"),
                "user": turn.get("user", ""),
                "assistant": turn.get("assistant", ""),
                "files": turn.get("files", []),
            }) + "\n")
        return True
    except OSError:
        return False


def _log_repo_activity(session_id: str, activity: dict | None) -> None:
    """Append the per-turn repository report next to `delivery.log`.

    The `systemMessage` the Stop hook prints is ephemeral: Claude Code renders it as an attachment
    and never writes it to the session transcript, so without this there is nothing to go back and
    read. One line per repository, greppable, local display paths (this file never leaves the
    machine). Not rotated — same as `delivery.log`.
    """
    if not activity:
        return
    stamp = _iso_now()
    try:
        with open(os.path.join(_cache_dir(), "repo-activity.log"), "a", encoding="utf-8") as fh:
            for entry in activity.get("repositories", []):
                fh.write(f"{stamp}  session={session_id}  repo={entry.get('repo', '')}  "
                         f"branch={entry.get('branch') or '(detached)'}  "
                         f"access={entry.get('access', '')}  path={entry.get('path', '')}\n")
            if activity.get("unresolved_paths"):
                fh.write(f"{stamp}  session={session_id}  "
                         f"unresolved={activity['unresolved_paths']}\n")
    except OSError:
        pass


def _is_git(cwd: str) -> bool:
    return _git(cwd, "rev-parse", "--is-inside-work-tree") == "true"


def _repo_name(cwd: str) -> str | None:
    """Origin URL's final path segment (the URL itself is never sent), else the folder name."""
    origin = _git(cwd, "remote", "get-url", "origin")
    if origin:
        seg = origin.strip().rstrip("/").rsplit("/", 1)[-1]
        if seg.endswith(".git"):
            seg = seg[:-4]
        if seg:
            return seg
    return os.path.basename(cwd.rstrip("/")) or None


def _branch_or_none(cwd: str) -> str | None:
    b = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return None if (not b or b == "HEAD") else b  # "HEAD" == detached


def _full_commit(cwd: str) -> str | None:
    return _git(cwd, "rev-parse", "HEAD")


def _repo_slug(cwd: str) -> str | None:
    """``org/repo`` from the origin URL — the shape repository-memory events want for repo_name.

    Both remote spellings reduce to the last two path segments (the host and any credentials are
    dropped, so the URL itself is still never sent)::

        https://github.com/org/repo.git  ->  org/repo
        git@github.com:org/repo.git      ->  org/repo

    Falls back to ``_repo_name`` (bare repo/folder name) when there is no origin to read.
    """
    origin = _git(cwd, "remote", "get-url", "origin")
    if origin:
        path = origin.strip().rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        path = path.split("://", 1)[-1]        # drop scheme
        if "@" in path and ":" in path:        # scp-style git@host:org/repo
            path = path.split(":", 1)[-1]
        segs = [s for s in path.split("/") if s]
        if len(segs) >= 2:
            return "/".join(segs[-2:])
        if segs:
            return segs[-1]
    return _repo_name(cwd)


def _cached_author() -> str:
    """The user_name the gateway returned at login, if connect.py stored one."""
    try:
        with open(os.path.join(_cache_dir(), "gateway-auth.json"), encoding="utf-8") as fh:
            return str(json.load(fh).get("user_name") or "").strip()
    except (OSError, json.JSONDecodeError, ValueError):
        return ""


def _author_name(cwd: str) -> str | None:
    """author_name for repository events: explicit override, else the git identity, else the
    owner, else whoever the gateway said you were at login.

    The login name is LAST on purpose. It is arguably the better identity — it is the same on
    every machine, where git user.name is not — but promoting it would silently move existing
    installs to a different author node, splitting their history. It is a floor, not a default.

    Returning None here means the event ships author_name=None and the server drops the turn,
    so the empty case warns instead of failing mute."""
    for value in (
        os.environ.get("VONIC_AUTHOR_NAME", "").strip(),
        (_git(cwd, "config", "user.name") or "").strip(),
        os.environ.get("VONIC_BRAIN_OWNER", "").strip(),
        _cached_author(),
    ):
        if value:
            return value
    _warn("no author identity: set VONIC_AUTHOR_NAME or git user.name — turns will be dropped")
    return None


def _git_identity(cwd: str) -> dict:
    """The repository scope a ``vonic_remember`` fact needs: ``{repo, branch, author, commit}``.

    Captured per turn at record time rather than read at delivery time. Delivery can happen minutes
    later, after a commit or a branch switch, and a turn must be attributed to the repository state
    it actually happened in. It also keeps a retried turn byte-identical: the server stores
    ``(tenant, event_id)`` once and treats the same id carrying different content as a hard
    conflict, so drifting metadata would turn a retry into a permanent failure.

    ``{}`` when ``cwd`` is not a work tree — see `_identity_scoped`.
    """
    if not _is_git(cwd):
        return {}
    return {
        "repo": _repo_slug(cwd),
        "branch": _branch_or_none(cwd) or "detached",   # branch_name needs 1+ chars
        "author": _author_name(cwd),
        "commit": _full_commit(cwd),
    }


def _identity_scoped(ident: dict) -> bool:
    """Whether an identity can scope a repository event. ``commit`` is optional; the rest are not.

    An unscoped fact routes to the *general* brain, and repository memory is the only brain
    codecollab may write to, so an unscoped turn is discarded rather than delivered.
    """
    return bool(ident.get("repo") and ident.get("branch") and ident.get("author"))


def _strip_code_rest(text: str) -> str:
    """Sanitize to prose: strip fenced, indented, and inline Markdown code."""
    text = _FENCE_RE.sub(" ", text)
    text = text.replace("[code omitted]", " ")  # marker the MCP-path strip leaves
    text = _INDENT_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)   # collapse gaps left by inline removal
    text = re.sub(r" +([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _rest_text(raw: str) -> str:
    t = _strip_code_rest(raw or "")
    cap = _env_int("VONIC_CODECOLLAB_MAX_CHARS", 100_000)
    return t if cap <= 0 or len(t) <= cap else t[:cap] + "…"


# ── transcript parsing ───────────────────────────────────────────────────────

def _blocks_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text", "")) for b in content
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        )
    return ""


def _is_human_prompt(line: dict) -> bool:
    if line.get("type") != "user":
        return False
    content = line.get("message", {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(isinstance(b, dict) and b.get("type") == "text" for b in content)
    return False


def _is_login_command(text: str) -> bool:
    """Login turns contain auth workflow output and are never useful repository memory."""
    return bool(_LOGIN_COMMAND_RE.search(text or ""))


def _tool_use_files(content: object) -> list[str]:
    paths: list[str] = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in _EDIT_TOOLS:
                inp = b.get("input") or {}
                p = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
                if p and p not in paths:
                    paths.append(str(p))
    return paths


# ── opt-in code + thinking capture (default OFF) ─────────────────────────────
#
# Both flags REVERSE the plugin's "never sent" privacy default (code and Claude's private
# thinking are otherwise stripped), so they are per-install, off unless explicitly enabled, and
# belong in the documented privacy contract. Enabling either also needs server schema support:
# RepositoryActivityEvent is extra="forbid", so the `reasoning` / `code_changes` keys emitted by
# _remember_metadata must exist server-side or every event is rejected. Capture stays prose-only
# in `content` (the synthesised field); code/thinking ride along as separate, non-synthesised
# fields so the ingestion budget is untouched.

def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


def _capture_thinking() -> bool:
    return _flag("VONIC_CAPTURE_THINKING")


def _capture_code() -> bool:
    return _flag("VONIC_CAPTURE_CODE")


def _thinking_text(content: object) -> str:
    """Assistant `thinking` blocks joined (the transcript stores the text under `thinking`)."""
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(b.get("thinking", "")) for b in content
        if isinstance(b, dict) and b.get("type") == "thinking" and b.get("thinking")
    )


def _tool_use_edits(content: object) -> list[dict]:
    """Change bodies from tool_use edit inputs (paths alone are already in _tool_use_files):
    [{path, before?, after?, content?, edits?}]."""
    edits: list[dict] = []
    if not isinstance(content, list):
        return edits
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_use"
                and b.get("name") in _EDIT_TOOLS):
            continue
        inp = b.get("input") or {}
        path = inp.get("file_path") or inp.get("path") or inp.get("notebook_path")
        if not path:
            continue
        rec: dict = {"path": str(path)}
        if inp.get("old_string") is not None or inp.get("new_string") is not None:   # Edit
            rec["before"] = str(inp.get("old_string", ""))
            rec["after"] = str(inp.get("new_string", ""))
        elif isinstance(inp.get("edits"), list):                                     # MultiEdit
            rec["edits"] = [
                {"before": str(e.get("old_string", "")), "after": str(e.get("new_string", ""))}
                for e in inp["edits"] if isinstance(e, dict)
            ]
        elif inp.get("content") is not None:                                         # Write / Create
            rec["content"] = str(inp["content"])
        elif inp.get("new_source") is not None:                                      # NotebookEdit
            rec["content"] = str(inp["new_source"])
        edits.append(rec)
    return edits


def _fenced_code(text: str) -> list[dict]:
    """Fenced code blocks pulled from assistant prose: [{language, text}]."""
    out = []
    for m in _FENCE_CAPTURE_RE.finditer(text or ""):
        body = m.group("body")
        if body and body.strip():
            out.append({"language": (m.group("lang") or "").strip() or None, "text": body})
    return out


# Best-effort secret scrub for the opt-in payloads. Not a guarantee — defense-in-depth on data
# the privacy default would otherwise never send.
_REDACTORS = (
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "«redacted-token»"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "«redacted-token»"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "«redacted-jwt»"),
    (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|authorization|bearer)\b"
                r"(\s*[:=]\s*)['\"]?[^\s'\"]{6,}['\"]?"), r"\1\2«redacted»"),
)


def _redact(text: str) -> str:
    for rx, repl in _REDACTORS:
        text = rx.sub(repl, text)
    return text


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[truncated]"


def _redact_edit(rec: dict) -> dict:
    return {k: (_redact(v) if k in ("before", "after", "content", "text") and isinstance(v, str) else v)
            for k, v in rec.items()}


def _prep_code(edits: list[dict], assistant_raw: str) -> list[dict]:
    """Merge tool-input edits + prose fences, redact, and cap the per-turn code budget so one huge
    diff can't bloat a turn."""
    items = [_redact_edit(e) for e in edits] + [_redact_edit(c) for c in _fenced_code(assistant_raw)]
    cap = _env_int("VONIC_MAX_CODE", 40_000)
    out, spent = [], 0
    for it in items:
        size = sum(len(str(v)) for k, v in it.items() if k != "path")
        if cap > 0 and spent + size > cap:
            break
        out.append(it)
        spent += size
    return out


def _parse_session(transcript_path: str) -> list[dict]:
    """Return ordered turns: [{user, assistant, files}]."""
    try:
        with open(transcript_path, encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError as exc:
        _debug(f"cannot read transcript: {exc}")
        return []
    turns: list[dict] = []
    cur: dict | None = None
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except json.JSONDecodeError:
            continue
        content = line.get("message", {}).get("content")
        if _is_human_prompt(line):
            if cur:
                turns.append(cur)
            # `cwd` rides on every transcript line; keep the turn's own so scope is per-turn,
            # not one directory for the whole session (a session can span several repos).
            user_text = _blocks_text(content)
            cur = {"user": user_text, "assistant": [], "files": [],
                   "thinking": [], "code": [], "cwd": line.get("cwd"),
                   "excluded": _is_login_command(user_text)}
        elif line.get("type") == "assistant" and cur is not None:
            if cur.get("cwd") is None:
                cur["cwd"] = line.get("cwd")   # human prompt lacked one; use the reply's
            txt = _blocks_text(content)
            if txt:
                cur["assistant"].append(txt)
            for p in _tool_use_files(content):
                if p not in cur["files"]:
                    cur["files"].append(p)
            if _capture_thinking():
                th = _thinking_text(content)
                if th:
                    cur["thinking"].append(th)
            if _capture_code():
                cur["code"].extend(_tool_use_edits(content))
    if cur:
        turns.append(cur)
    turns = [t for t in turns if not t.pop("excluded", False)]
    for t in turns:
        t["assistant"] = "\n".join(t["assistant"]).strip()
        t["thinking"] = "\n\n".join(t.get("thinking", [])).strip()
    return turns


# ── buffer (locked) ──────────────────────────────────────────────────────────

def _buffer_path(session_id: str) -> str:
    return os.path.join(_sessions_dir(), f"{_slugify(session_id) or 'session'}.json")


class _locked:
    """Exclusive flock around a buffer file so deliveries never race."""

    def __init__(self, path: str):
        self.path = path
        self._fh = None

    def __enter__(self):
        self._fh = open(self.path + ".lock", "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def _prune_orphan_locks() -> None:
    """Drop `.lock` files whose buffer is gone, so never unlinking a held lock cannot leak them.

    Safe only from `sweep`, and only under both guards: a lock with no buffer beside it has no
    deliverer to protect (`_deliver` returns immediately when the buffer is missing), and the age
    gate keeps a lock created moments ahead of its buffer from being taken out underneath it.
    """
    cutoff = time.time() - 86_400
    for lock in glob.glob(os.path.join(_sessions_dir(), "*.json.lock")):
        try:
            if os.path.exists(lock[: -len(".lock")]) or os.path.getmtime(lock) >= cutoff:
                continue
            os.remove(lock)
        except OSError:
            pass


def _load_buffer(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_buffer(path: str, buf: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(buf, fh)
    os.replace(tmp, path)


_CLIENT_LABELS = {"cc": "Claude Code", "cx": "Codex", "oc": "Opencode", "cur": "Cursor"}

_PLUGIN_VERSION: str | None = None


def _plugin_root() -> str:
    """The plugin directory this file was loaded from — the manifest dir, one level above `scripts/`.

    Identity, not just a lookup path: it is what distinguishes two INSTALLED COPIES of the same
    plugin (a directory-source marketplace pointing at a work tree vs. the cache snapshot installed
    from it). They share a version string; they do not share a root.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _plugin_version() -> str:
    """This plugin's version, self-reported into logs + session buffers so the running BUILD is
    verifiable (which cached copy actually fired), not just what `plugin list` claims.

    The deliverer is vendored into different layouts — Claude `.claude-plugin/`, Codex `.codex-plugin/`,
    Opencode `package.json` — so try each manifest relative to this file's parent dir. Cached; best-effort
    ('?' if none found). The buffer copy rides to the backend in the free-form ``metadata`` bag as
    ``client_version``, feeding fleet/version reporting.
    """
    global _PLUGIN_VERSION
    if _PLUGIN_VERSION is not None:
        return _PLUGIN_VERSION
    parent = _plugin_root()
    ver = "?"
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "package.json"):
        try:
            with open(os.path.join(parent, rel), encoding="utf-8") as fh:
                ver = json.load(fh).get("version") or "?"
            break
        except Exception:  # noqa: BLE001 — best-effort; never break capture over a version read
            continue
    _PLUGIN_VERSION = ver
    return ver


_BUILD: dict | None = None


def _build() -> dict:
    """Which BUILD of this deliverer is running: ``{version, root, sha}``.

    `_plugin_version` was meant to answer "which cached copy actually fired" and cannot. Two roots
    can carry the SAME version and DIFFERENT code — a directory-source marketplace pointing at a
    work tree, plus the cache snapshot installed from it at an older commit — and then `plugin
    list`, the logs and the buffer all agree on one version while a pre-feature build is what
    actually delivers. That is not hypothetical: it is how opt-in code/thinking capture appeared
    dead for a day while its flags were set and its collection path was running.

    So identity needs both halves. ``root`` separates two COPIES; ``sha`` (a digest of this file)
    separates two STATES of one copy, which is what an editable dev checkout is. Local-only —
    buffer + delivery.log. It is deliberately NOT on the wire: `RepositoryActivityEvent` is
    extra="forbid", so a new key must land server-side first.
    """
    global _BUILD
    if _BUILD is not None:
        return _BUILD
    sha = "?"
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()[:12]
    except OSError:  # best-effort; provenance must never break capture
        pass
    _BUILD = {"version": _plugin_version(), "root": _plugin_root(), "sha": sha}
    return _BUILD


def _check_build(buf: dict, session_id: str) -> None:
    """Warn when a buffer written by a DIFFERENT plugin root is being touched by this one.

    Two roots sharing `~/.cache/codecollab` is the failure this exists to surface: both record into
    one buffer and both deliver from it, and since `uploaded` is a one-way latch and `event_id`
    (`session:seq`) makes a turn's content immutable once accepted, whichever copy reaches the
    server first decides that turn's format permanently. The loser cannot correct it later — a
    re-send with different content is a hard conflict, not an update.

    Detection only, no behaviour change. Refusing to deliver would strand turns whenever the owning
    copy stops running, and namespacing the cache per root would not help either: both copies would
    still emit the same `event_id` with different bodies, trading silent format loss for delivery
    conflicts. The fix is to stop having two roots; this makes that condition legible in seconds
    rather than a day.
    """
    mine = _build()
    theirs = buf.get("build") or {}
    if not theirs or theirs.get("root") == mine["root"]:
        return
    warn = (f"duplicate plugin root: buffer written by {theirs.get('root')} "
            f"(v{theirs.get('version')} sha={theirs.get('sha')}), "
            f"this process is {mine['root']} (v{mine['version']} sha={mine['sha']})")
    _debug(warn)
    _log_delivery(session_id, "-", "build", "", f"WARNING {warn}")


def _new_buffer(session_id: str, cwd: str, client_tag: str = "cc") -> dict:
    project = _slugify(os.path.basename(cwd.rstrip("/"))) or "project"
    date = time.strftime("%Y-%m-%d", time.gmtime())
    sid8 = _slugify(session_id)[:8] or "session"
    return {
        "session_id": session_id,
        "slug": f"notes/{date}-{client_tag}-{project}-{sid8}",
        "project": project,
        "cwd": cwd,
        "date": date,
        "client_tag": client_tag,
        "client": _CLIENT_LABELS.get(client_tag, "Claude Code"),
        "plugin_version": _plugin_version(),
        "build": _build(),   # which COPY is writing — see `_check_build`
        "branch": _git(cwd, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git(cwd, "rev-parse", "--short", "HEAD"),
        "turns_recorded": 0,
        "turns": [],
        "done": False,
    }


# ── page + entry content ─────────────────────────────────────────────────────

def _page_body(buf: dict) -> str:
    """The FULL session as one markdown page: prompts + replies in the body, so
    gbrain's distiller and `think` (which read page bodies) can use it."""
    turns = buf.get("turns", [])
    client = buf.get("client", "Claude Code")
    client_slug = client.lower().replace(" ", "-")
    all_files: list[str] = []
    for t in turns:
        for f in t.get("files", []):
            if f not in all_files:
                all_files.append(f)
    fm = [
        "---", "type: note",
        f'title: "{client} session — {buf["project"]} {buf["date"]}"',
        f"owner: {os.environ.get('VONIC_BRAIN_OWNER', 'unknown')}",
        "source: manual", "distilled: false",
        f"tags: [{client_slug}, code-session]",
        f"project: {buf['project']}",
        f"session_id: {buf['session_id']}",
        f"created: {buf['date']}",
    ]
    if all_files:
        fm.append("files: [" + ", ".join(json.dumps(f) for f in all_files) + "]")
    if buf.get("branch"):
        fm.append(f"branch: {buf['branch']}")
    if buf.get("commit"):
        fm.append(f"commit: {buf['commit']}")
    fm.append("---")
    body = [
        f"\n# {client} session — {buf['project']} {buf['date']}\n",
        f"> Prompts and the assistant's text replies from a {client} session "
        "(no code, patches, or thinking).\n",
    ]
    if all_files:
        body.append(f"Files touched: {', '.join(all_files)}\n")
    for t in turns:
        body.append(f"## Turn {t.get('seq', 0) + 1}\n")
        if t.get("user"):
            body.append(f"**User:** {t['user']}\n")
        if t.get("assistant"):
            body.append(f"**Assistant:** {t['assistant']}\n")
    return "\n".join(fm) + "\n" + "\n".join(body)


# ── repository-event metadata (vonic_remember mode="fact") ───────────────────
#
# Mirrors vonic_agent's RepositoryRememberEventMetadata. Every model there is strict
# (extra="forbid"), so an unknown key rejects the event — same failure mode the REST contract
# in contracts/coding-agent-message.json exists to prevent. Keep the two in sync.

_EVENT_TYPES = {
    "git_commit", "decision_taken", "clarification_asked", "code_explanation",
    "code_change", "review_feedback", "plan_created", "other",
}
_MAX_EVENT_CONTENT = 100_000   # RepositoryActivityEvent.content max_length
_MAX_CHANGED_FILES = 1_000     # RepositoryActivityEvent.changed_files max_length
_MAX_PATH = 1_024              # ChangedFile.path max_length
_MAX_REPOS_TOUCHED = 50        # metadata bag is unbounded server-side; bound it here anyway


def _event_type(turn: dict) -> tuple[str, str | None]:
    """``(event_type, other_type)`` for one captured turn.

    A turn that edited files is a ``code_change``; one that edited nothing is a prompt answered in
    prose, i.e. ``code_explanation``. ``VONIC_EVENT_TYPE`` pins a single type for every turn; an
    unrecognised value degrades to ``other`` + ``other_type`` (which the server requires for
    ``other``) rather than failing the event.
    """
    override = os.environ.get("VONIC_EVENT_TYPE", "").strip()
    if override:
        if override in _EVENT_TYPES and override != "other":
            return override, None
        return "other", (override if override != "other" else "coding_session_turn")
    return ("code_change" if turn.get("files") else "code_explanation"), None


def _turn_content(turn: dict) -> str:
    """One turn as prose — the event's ``content`` text. Already sanitised by ``_record_turns``."""
    parts = []
    if turn.get("user"):
        parts.append(f"**User:** {turn['user']}")
    if turn.get("assistant"):
        parts.append(f"**Assistant:** {turn['assistant']}")
    return "\n\n".join(parts).strip()[:_MAX_EVENT_CONTENT]


def _event_content(turn: dict):
    """The ``vonic_remember`` ``content`` for one turn.

    Default (opt-in flags off, or nothing captured): a **bare prose string** — byte-identical to
    what the whole fleet already sends, so the server's ``content: str`` path is unchanged.

    When VONIC_CAPTURE_THINKING / VONIC_CAPTURE_CODE captured something, ``content`` is instead a
    structured object ``{text_content, reasoning?, code_changes?}`` (Amitoj's shape): the prose
    stays in ``text_content`` — the only field the ingestion agent synthesises — while thinking and
    code ride along as sibling fields, stored verbatim and never fed to the LLM. Returns ``None``
    when there is no prose after sanitising (the turn is dropped, as before)."""
    text = _turn_content(turn)
    if not text:
        return None
    reasoning = turn.get("thinking") or ""
    code = turn.get("code") or []
    if not reasoning and not code:
        return text
    content = {"text_content": text}
    if reasoning:
        content["reasoning"] = reasoning
    if code:
        content["code_changes"] = code
    return content


def _remember_metadata(buf: dict, turn: dict, repo: str, branch: str, author: str,
                       commit: str | None) -> dict:
    """``metadata.event`` for one turn — the repository identity ``vonic_remember`` requires.

    ``event_id`` is the server's idempotency key: it stores ``(tenant, event_id)`` once and
    compares a content hash on re-delivery. Re-sending identical content is a harmless
    ``duplicate``, but the SAME id carrying DIFFERENT content is a hard ``conflict`` that raises.
    ``session:seq`` is stable per turn and a turn's text never changes after capture, so a delivery
    retried after a failure dedups instead of erroring — which is also why each turn is its own
    event rather than the whole session being re-sent under one id every time.
    """
    event_type, other_type = _event_type(turn)
    event = {
        "event_id": f"{buf.get('session_id', 'session')}:{turn.get('seq', 0)}"[:128],
        "event_type": event_type,
        "repo_name": repo,
        "branch_name": branch,
        "author_name": author,
        "occurred_at": turn.get("ts") or _iso_now(),   # tz-aware: naive stamps are rejected
    }
    if other_type:
        event["other_type"] = other_type
    if commit:
        event["git_commit_id"] = commit
    repository_id = os.environ.get("VONIC_REPOSITORY_ID", "").strip()
    if repository_id:
        event["repository_id"] = repository_id
    author_id = (os.environ.get("VONIC_AUTHOR_ID", "").strip()
                 or os.environ.get("VONIC_USER_ID", "").strip())
    if author_id:
        event["author_id"] = author_id
    files = [str(f)[:_MAX_PATH] for f in turn.get("files", []) if f][:_MAX_CHANGED_FILES]
    if files:
        # additions/deletions are not captured (the plugin never reads diffs); the server
        # defaults them to 0 and `status` stays unset rather than guessed.
        event["changed_files"] = [{"path": f} for f in files]
    # Free-form bag: the session linkage that used to live in the page frontmatter.
    #
    # `client_version` is the build that CAPTURED the turn, not the one delivering it — a buffer
    # written by an older copy can be delivered by a newer one, and the question the fleet view
    # asks ("what is this member running?") is about the former. Falls back to the running build
    # for buffers written before the field existed.
    event["metadata"] = {
        "session_id": buf.get("session_id", ""),
        "turn_index": turn.get("seq", 0),
        "client": buf.get("client", ""),
        "client_version": buf.get("plugin_version") or _plugin_version(),
        "project": buf.get("project", ""),
    }
    _attach_repo_activity(event["metadata"], turn)
    return {"event": event}


def _attach_repo_activity(bag: dict, turn: dict) -> None:
    """Add the turn's repository touches to the free-form ``metadata`` bag.

    THE BAG, AND ONLY THE BAG. Every model behind ``metadata.event`` is ``extra="forbid"``, so a
    new key on ``event`` itself is a breaking cross-repo change that must land server-first — that
    is the v0.12.0 ``repositoriesTouched`` incident, recorded in docs/DECISIONS.md. ``metadata`` is
    a free-form dict and already carries client-defined keys (session_id, turn_index, client,
    project), so extending it here rejects nothing.

    Slug + branch + access only. The local display path stays local: shipping it would leak this
    machine's directory layout for checkouts this event is not even scoped to. Read-only paths do
    not, and must not, reach ``changed_files`` — that list still comes from ``turn["files"]``.

    ``VONIC_REPO_ACTIVITY_WIRE=0`` is the kill switch. It is deliberately its own flag: 0.12.0's
    root cause was one boolean guarding two payload extensions with opposite compatibility.
    """
    if os.environ.get("VONIC_REPO_ACTIVITY_WIRE", "1") == "0":
        return
    activity = turn.get("repos") or {}
    entries = [
        {"repo": e.get("repo", ""), "branch": e.get("branch"),
         "access": (e.get("access") if e.get("access") in {"read", "write", "read_write"}
                    else "read")}
        for e in activity.get("repositories", [])[:_MAX_REPOS_TOUCHED] if e.get("repo")
    ]
    if entries:
        bag["repositories_touched"] = entries
    if activity.get("unresolved_paths"):
        bag["unresolved_path_count"] = activity["unresolved_paths"]


# ── gbrain target resolution ─────────────────────────────────────────────────

def _secret(base: str) -> str:
    """Resolve a secret without keeping it in plaintext env, in precedence order:

      1. ``$BASE``          — the literal value (dev / Mode B)
      2. ``$BASE_CMD``      — a shell command whose stdout is the secret; this is the
                              vault hook (e.g. ``vault read -field=token secret/codecollab``,
                              ``op read op://vault/codecollab/token``, ``aws secretsmanager …``)
      3. ``$BASE_FILE``     — a file to read the secret from (e.g. a tmpfs-mounted secret)

    So a production install can keep the gbrain/gateway token in a secret manager and never
    write it into ~/.claude/settings.json."""
    direct = os.environ.get(base, "").strip()
    if direct:
        return direct
    cmd = os.environ.get(f"{base}_CMD", "").strip()
    if cmd:
        try:
            out = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=_env_int("VONIC_CODECOLLAB_SECRET_TIMEOUT", 10),
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
            _debug(f"{base}_CMD exit={out.returncode}: {out.stderr.strip()[:120]}")
        except (OSError, subprocess.SubprocessError) as exc:
            _debug(f"{base}_CMD failed: {exc}")
    path = os.environ.get(f"{base}_FILE", "").strip()
    if path:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            _debug(f"{base}_FILE read failed: {exc}")
    return ""


def _identity_from_token() -> bool:
    """Mode A (production): writes go through the OAuth connector, which derives
    (tenant, user) from the *validated* gateway token. In that mode codecollab must NOT
    assert its own identity headers — the token is authoritative, headers would be spoofing.

    A cached login IMPLIES Mode A, so it is the default once you have logged in: the login
    token is then the only credential, and there is no configured tenant/user to assert, so
    Mode B cannot work by construction. Requiring VONIC_BECOS_IDENTITY_FROM_TOKEN=1 on top
    of a successful login meant `/codecollab:login` reported success while delivery stayed
    blocked — the flag was asking the user to declare something already knowable.

    VONIC_BECOS_IDENTITY_FROM_TOKEN still forces either mode explicitly ("1" / "0")."""
    flag = os.environ.get("VONIC_BECOS_IDENTITY_FROM_TOKEN", "").strip()
    if flag:
        return flag == "1"
    return bool(_cached_gateway())


def _gateway_token(target: str = "") -> str:
    """The gateway session token, PINNED to the gateway it was minted for.

    The cached token mints JWTs for the whole tenant, so leaking it is worse than leaking
    one session's turns. `target` is the URL the token is about to be presented to: if it
    resolves to a different host than the login that produced the token, withhold it. That
    way a poisoned VONIC_GATEWAY_URL can still be pointed somewhere else, but it cannot
    walk off with the credential to the team's existing memory.

    An explicit VONIC_GATEWAY_TOKEN is exempt — whoever set it owns the pairing.
    """
    token = _secret("VONIC_GATEWAY_TOKEN")
    if token:
        return token
    try:
        with open(os.path.join(_cache_dir(), "gateway-auth.json"), encoding="utf-8") as fh:
            auth = json.load(fh)
        cached_token = str(auth.get("token", "")).strip()
        cached_gateway = str(auth.get("gateway", "")).strip()
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    if target and cached_gateway and _url_host(target) != _url_host(cached_gateway):
        _warn(f"refusing to send your {_url_host(cached_gateway)} session token to "
              f"{_url_host(target) or 'an unparseable host'} — run /codecollab:login "
              f"against that gateway if the change is intentional")
        return ""
    return cached_token


def _url_host(url: str) -> str:
    """Lowercased hostname, or "" if the URL is unparseable."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _valid_gateway(url: str, source: str) -> str:
    """Return `url` if it is a safe transport target, else "" (with a warning).

    Mirrors connect.py's _validate_transport_url. capture.py skipped this check while
    connect.py enforced it, and capture.py is the side that ships turns AND presents the
    bearer token — so an http:// or credential-bearing URL from the environment put both
    on the wire. Invalid values are dropped, not fatal: resolution falls through to the
    next rung rather than breaking the session.
    """
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        _warn(f"ignoring malformed gateway URL from {source}")
        return ""
    host = parsed.hostname or ""
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        _warn(f"ignoring invalid gateway URL from {source}")
        return ""
    if parsed.scheme != "https" and not loopback:
        _warn(f"ignoring non-HTTPS gateway URL from {source} (would send your token in plaintext)")
        return ""
    return url


def _cached_gateway() -> str:
    """The gateway URL connect.py cached at login — the same file _gateway_token reads."""
    try:
        with open(os.path.join(_cache_dir(), "gateway-auth.json"), encoding="utf-8") as fh:
            cached = str(json.load(fh).get("gateway", "")).strip().rstrip("/")
    except (OSError, json.JSONDecodeError, ValueError):
        return ""
    return _valid_gateway(cached, "the cached login")


def _resolve_gateway() -> str:
    """Gateway base URL: explicit env, else the URL cached at login, else the hosted default.

    Mirrors connect.py's _resolve_gateway. Env is an OVERRIDE (self-hosted, enterprise, CI),
    not a requirement: requiring it made capture and recall no-op silently for anyone who
    installed the plugin and ran /codecollab:login without also running setup.sh.
    """
    env = _valid_gateway(os.environ.get("VONIC_GATEWAY_URL", "").strip().rstrip("/"),
                         "VONIC_GATEWAY_URL")
    cached = _cached_gateway()
    if env and cached and _url_host(env) != _url_host(cached):
        # Not fatal — staging and self-hosted installs do this deliberately — but it is
        # also exactly what a poisoned environment looks like, so never do it silently.
        _warn(f"VONIC_GATEWAY_URL ({_url_host(env)}) overrides the gateway you logged in "
              f"to ({_url_host(cached)}); captured turns will be sent to {_url_host(env)}")
    return env or cached or _DEFAULT_GATEWAY


def _resolve_becos_url() -> str:
    """becos MCP endpoint: explicit env, else <gateway>/mcp/ — the same default setup.sh writes."""
    return os.environ.get("VONIC_BECOS_URL", "").strip() or f"{_resolve_gateway()}/mcp/"


def _resolve_gbrain() -> tuple[str, str]:
    """Return (url, token). Direct env wins; else discover from the gateway (cached
    to the token's expiry — the gateway may hand out a SHORT-LIVED token)."""
    env_url = os.environ.get("VONIC_GBRAIN_URL", "").strip()
    env_token = _secret("VONIC_GBRAIN_TOKEN")
    if env_url and env_token:
        return env_url, env_token

    cache_file = os.path.join(_cache_dir(), "gbrain-config.json")
    try:
        with open(cache_file, encoding="utf-8") as fh:
            cached = json.load(fh)
        if time.time() < cached.get("expires_at", 0) - 30 and cached.get("gbrain_url"):
            return cached["gbrain_url"], env_token or cached.get("gbrain_token", "")
    except (OSError, json.JSONDecodeError, ValueError):
        pass

    gateway = _resolve_gateway()
    gw_token = _gateway_token(gateway)
    if not gateway or not gw_token:
        if env_url and (env_token or env_url):
            raise gbrain_client.GbrainError("missing gbrain token")
        raise gbrain_client.GbrainError(
            "no gbrain target: run /codecollab:login, or set VONIC_GBRAIN_URL/TOKEN"
        )
    req = urllib.request.Request(
        f"{gateway}/plugin/gbrain-config",
        headers={"Authorization": f"Bearer {gw_token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=_env_int("VONIC_CODECOLLAB_TIMEOUT", 15)) as resp:
        cfg = json.loads(resp.read())
    url = env_url or str(cfg.get("gbrain_url", "")).strip()
    token = env_token or str(cfg.get("gbrain_token", "")).strip()
    # expires_at from the gateway (short-lived token) or a default TTL.
    expires_at = float(cfg.get("expires_at") or (time.time() + _CONFIG_TTL))
    try:
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump({"gbrain_url": url, "gbrain_token": token, "expires_at": expires_at}, fh)
        os.chmod(cache_file, 0o600)
    except OSError:
        pass
    if not url:
        raise gbrain_client.GbrainError("no gbrain url resolved")
    if not token:
        raise gbrain_client.GbrainError("no gbrain token resolved")
    return url, token


# ── delivery (detached, locked) ──────────────────────────────────────────────

# How many times one turn may fail a becos delivery before it is parked in `dead-letter.log`.
# Only failures the server answered count (see `_deliver_becos`), so an outage never exhausts this.
_MAX_TURN_ATTEMPTS = 3

# Legacy servers reported an event-id collision only as an isError string. Structured receipts make
# conflict an explicit non-success, so this compatibility exception is used only when no receipt was
# available (call_tool raises before returning a legacy isError result).
_ALREADY_STORED = "event_id is already associated with a different event"


def _receipt_auth(receipt: gbrain_client.Receipt | None, message: str = "") -> bool:
    if receipt is not None:
        return receipt.acceptance == "rejected" and receipt.condition == "auth"
    text = message.lower()
    return "401" in text or "unauthorized" in text or "forbidden" in text


def _receipt_error(receipt: gbrain_client.Receipt) -> gbrain_client.GbrainError:
    detail = receipt.error or (
        f"acceptance={receipt.acceptance} state={receipt.state} outcome={receipt.outcome}"
    )
    return gbrain_client.GbrainError(
        f"structured receipt condition={receipt.condition}: {detail}",
        transport=False,
    )


def _receipt_success(receipt: gbrain_client.Receipt) -> str | None:
    """Ownership/finalization disposition for a valid taxonomy receipt."""
    if receipt.acceptance == "accepted" and receipt.state == "pending":
        return "accepted"
    if (receipt.acceptance == "accepted" and receipt.state == "terminal"
            and receipt.outcome == "succeeded"):
        return "succeeded"
    return None


def _backfill_handoff(
    result: dict, *, operation: str, batch_id: str, session_id: str, turns: int,
) -> str:
    """Validate taxonomy or the connector origin/main upload receipt before dropping local data."""
    receipt = gbrain_client.result_receipt(result, operation)
    if receipt.present:
        disposition = _receipt_success(receipt) if receipt.valid else None
        if disposition is None:
            raise _receipt_error(receipt)
        return f"{disposition}:{receipt.condition}"

    data = gbrain_client.tool_data(result)
    jobs = data.get("jobs")
    accepted = data.get("accepted_turns")
    if data.get("batch_id") != batch_id or type(accepted) is not int or accepted != turns:
        raise gbrain_client.GbrainError("legacy enqueue receipt did not accept all turns")
    if not isinstance(jobs, list):
        raise gbrain_client.GbrainError("legacy enqueue receipt omitted jobs")
    job = next((j for j in jobs if isinstance(j, dict) and j.get("session_id") == session_id), None)
    if (job is None or job.get("state") not in {"queued", "running", "complete"}
            or type(job.get("turns_total")) is not int or job["turns_total"] != turns):
        raise gbrain_client.GbrainError("legacy enqueue job did not retain all turns")
    return f"accepted:legacy-{job['state']}"


def _turn_sent(t: dict) -> bool:
    """REST: a turn is done when each of its present messages has been POSTed."""
    return (not t.get("user") or t.get("user_sent")) and (not t.get("assistant") or t.get("asst_sent"))


def _deliver_mcp(buf: dict, timeout: float) -> None:
    """gbrain transport: the whole session as one page BODY (put_page overwrites by slug)."""
    url, token = _resolve_gbrain()
    gbrain_client.put_page(url, token, buf["slug"], _page_body(buf), timeout)
    for t in buf.get("turns", []):
        t["uploaded"] = True


def _require_logged_in_host(url: str) -> None:
    """Deliver only to the gateway this machine logged in to. Raises otherwise.

    Pinning the TOKEN (see _gateway_token) stops a redirected gateway from STEALING the
    credential, but not from RECEIVING turns: every check before this one was about the
    credential, so an attacker who also set VONIC_BECOS_TOKEN supplied their own and walked
    past them with the destination unexamined. This asks the question none of them asked —
    *where* — and asks it before any token is resolved, so bringing your own token is no
    longer a way around it.

    The pin is per-install, not a vendor allowlist: it is whatever host /codecollab:login
    recorded, so a self-hosted gateway pins to itself and would refuse becos.ai. Changing it
    is a deliberate act — log in again against the new gateway.

    Never logged in (Mode B: an explicitly configured URL + token) means there is nothing to
    pin against, and that configuration is left alone by design.
    """
    home = _url_host(_cached_gateway())
    if not home:
        return
    target = _url_host(url)
    if target == home:
        return
    _warn(f"refusing to deliver to {target or 'an unparseable host'}: this machine is "
          f"logged in to {home}")
    raise _config_error(
        f"refusing to deliver to {target or 'an unparseable host'}: this machine is logged "
        f"in to {home}. Run /codecollab:login against the new gateway if that is intended."
    )


def _config_error(message: str) -> gbrain_client.GbrainError:
    """A delivery failure caused by SETUP rather than by the network or the server.

    `_deliver` reports transient failures through `_debug` — an outage is temporary and
    shouting every turn would be noise — but these through `_warn`. A misconfigured install
    never fixes itself, and staying quiet about it is how three separate setup gaps each
    presented as "the plugin captures nothing and says nothing"."""
    exc = gbrain_client.GbrainError(message)
    exc.config = True
    return exc


_MINT_LEEWAY = 60  # refresh this many seconds before the minted JWT actually expires


def _mint_becos_jwt(gateway: str) -> str:
    """Exchange the cached login session for a SHORT-LIVED JWT at ``<gateway>/oauth/token``.

    The becos MCP endpoint does not accept the long-lived session token — it answers 401. It
    wants a fresh JWT, and only setup.sh's mint script produced one (wired in as
    VONIC_BECOS_TOKEN_CMD), so an install done the way plugin.json advertises — /plugin install
    then /codecollab:login — presented the session token and was rejected on every delivery and
    every recall. Nothing was lost (turns stayed buffered) but nothing was ever delivered.

    Minting here makes the mint script optional rather than load-bearing. An explicit
    VONIC_BECOS_TOKEN / *_CMD still wins, so existing setup.sh installs keep their own path.

    Cached to just before its expiry: a hook runs per turn, and a mint round-trip per turn
    would put avoidable latency on the session. Returns "" on any failure — the caller then
    reports "no becos token", which is a config error and now warns rather than whispering.
    """
    session = _gateway_token(gateway)
    if not session:
        return ""
    cache_file = os.path.join(_cache_dir(), "becos-jwt.json")
    try:
        with open(cache_file, encoding="utf-8") as fh:
            cached = json.load(fh)
        if (cached.get("gateway") == gateway and cached.get("jwt")
                and time.time() < float(cached.get("expires_at", 0)) - _MINT_LEEWAY):
            return str(cached["jwt"])
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    req = urllib.request.Request(
        f"{gateway}/oauth/token",
        data=b"{}",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {session}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_env_int("VONIC_CODECOLLAB_TIMEOUT", 15)) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        _debug(f"becos jwt mint failed: {exc}")  # transient: the caller reports the config error
        return ""
    jwt = str(body.get("access_token") or "").strip()
    if not jwt:
        _debug("becos jwt mint returned no access_token")
        return ""
    try:
        expires_in = float(body.get("expires_in") or _CONFIG_TTL)
    except (TypeError, ValueError):
        expires_in = float(_CONFIG_TTL)
    try:
        with open(cache_file, "w", encoding="utf-8") as fh:
            json.dump({"jwt": jwt, "gateway": gateway, "expires_at": time.time() + expires_in}, fh)
        os.chmod(cache_file, 0o600)
    except OSError:
        pass
    return jwt


def _resolve_becos() -> tuple[str, str]:
    """becos target: the resolved becos URL + a token (VONIC_BECOS_TOKEN or the gbrain token).

    Two independent gates, in this order: the DESTINATION must be the logged-in gateway, and
    then a TOKEN must exist. Destination first — a token check cannot police where data goes,
    because the attacker chooses the token.
    """
    url = _resolve_becos_url()
    _require_logged_in_host(url)
    token = _secret("VONIC_BECOS_TOKEN") or _secret("VONIC_GBRAIN_TOKEN")
    if not token and _identity_from_token():
        # Mode A: the connector validates a SHORT-LIVED JWT and derives identity from its
        # claims. Mint one from the cached login — the session token itself is refused with
        # 401 here, which is why a login-only install delivered nothing at all. Falls back to
        # the session token if minting fails, so a gateway that accepts it directly still
        # works and this is not a new hard dependency.
        token = _mint_becos_jwt(_resolve_gateway()) or _gateway_token(url)
    if not token:
        raise _config_error(
            "no becos token: run /codecollab:login, or set VONIC_BECOS_TOKEN / "
            "VONIC_GBRAIN_TOKEN (or *_CMD/_FILE)"
        )
    return url, token


def _becos_identity_headers() -> dict:
    """Identity forwarded to the becos surface on the direct/internal path.

    In production the OAuth connector fronts the write path and derives identity from the
    token (authoritative). When codecollab talks to vonic_agent directly, we forward the
    configured ids so the ingest receipt is scoped to a real tenant/user instead of the
    server default. Empty values are omitted (vonic_agent then falls back to its default).
    """
    if _identity_from_token():
        return {}  # Mode A: the connector derives (tenant, user) from the validated token
    tenant = os.environ.get("VONIC_TENANT_ID", "").strip()
    user = (
        os.environ.get("VONIC_USER_ID", "").strip()
        or os.environ.get("VONIC_BRAIN_OWNER", "").strip()
    )
    headers = {}
    if tenant:
        headers["X-Tenant-ID"] = tenant
    if user:
        headers["X-User-ID"] = user
    return headers


def _build_events(buf: dict) -> list[dict]:
    """The deliverable events for a buffer: the per-turn ``vonic_remember(mode="fact")`` args,
    built once and shared by both delivery paths — the live/sync ``_deliver_becos`` below and the
    async backfill upload in ``backfill.py``. Returns ``[{"event_id", "args", "turn"}]`` for the
    turns that should ship; as a side effect it marks the non-deliverable ones ``uploaded``
    (nothing left after sanitising, or unscoped — which would hit the general brain codecollab may
    not write) and logs those drops, exactly as the inline loop did. Returns ``[]`` when the whole
    buffer is unscoped, so a caller may read empty as 'nothing to deliver'.

    This is the single source of truth for the strict ``RepositoryRememberEventMetadata`` shape —
    keeping it in one place is what the 'keep the two in sync' warnings elsewhere are about."""
    session_id = buf.get("session_id", "")
    turns = buf.get("turns", [])
    # Turns recorded before 0.18.0 carry no identity snapshot; the live work tree is their fallback.
    live = _git_identity(buf.get("cwd", ""))
    if not _identity_scoped(live) and not any(_identity_scoped(t.get("git") or {}) for t in turns):
        dropped = sum(1 for t in turns if not t.get("uploaded"))
        _debug(f"no repository scope ({live!r}) — dropping {dropped} unscoped turn(s)")
        for t in turns:
            t["uploaded"] = True   # discard: unscoped writes would hit the general brain
        return []
    events: list[dict] = []
    for t in turns:
        if t.get("uploaded"):
            continue
        content = _event_content(t)
        if content is None:
            t["uploaded"] = True   # nothing left after sanitising
            continue
        seq = t.get("seq", 0)
        # A turn carries its OWN scope snapshot (`git`). Only turns recorded before per-turn scope
        # (no `git` key at all) fall back to the session work tree — a turn WITH a snapshot is
        # honoured strictly: an empty one means that turn's own cwd was outside any repo, and it is
        # dropped below rather than mis-filed under the session's dominant repo (which would fold,
        # e.g., a deleted side-directory's turns into the wrong repository's memory).
        git = t["git"] if "git" in t else live
        if not _identity_scoped(git):
            t["uploaded"] = True
            _log_delivery(session_id, seq, "turn", "", "dropped: no repository scope")
            continue
        args = {
            "content": content,   # str by default; {text_content, reasoning?, code_changes?} when enriched
            "mode": "fact",
            "metadata": _remember_metadata(buf, t, git["repo"], git["branch"], git["author"],
                                           git.get("commit")),
        }
        events.append({"event_id": args["metadata"]["event"]["event_id"], "args": args, "turn": t})
    return events


def _deliver_becos(buf: dict, timeout: float, checkpoint=None) -> None:
    """becos transport: one `vonic_remember(mode="fact")` repository event per turn.

    ``mode`` is always ``fact`` — it is the only mode that accepts repository metadata
    (``raw``/``distil`` reject it outright), and repository ingestion is the point of the
    metadata. Each unsent turn goes as its own event keyed by ``session:seq``, so a delivery that
    fails part-way resumes at the first unsent turn and already-sent turns dedup server-side.

    Drops the session when it has no resolvable repo/branch/author — a cwd outside any git work
    tree. An unscoped fact routes to the *general* brain, and repository memory is the only brain
    codecollab may write to, so such turns are discarded (marked uploaded) rather than delivered
    unscoped. They are logged so the loss is visible.

    ONE TURN MUST NEVER BLOCK THE QUEUE BEHIND IT. Each turn's call is isolated: a failure records an
    attempt and moves on to the next turn. Letting the exception escape this loop is the bug this
    guards — the failing turn stayed unmarked and got re-POSTed on every later Stop hook (the same
    stale event, over and over), while every turn behind it was never delivered at all.

    Two failure shapes, told apart by ``GbrainError.transport``:

    * **outage** — nothing delivered and every failure was transport-class: the endpoint is down, so
      no turn's retry budget is spent and the whole buffer defers to the next hook, as before.
    * **poison turn** — a turn that keeps failing while others succeed: parked in `dead-letter.log`
      after ``_MAX_TURN_ATTEMPTS`` and marked uploaded, which is what finally stops the re-sends.

    ``checkpoint`` persists the buffer after each turn. A backlog of 20 turns is ~7 minutes of calls;
    without it, an interrupted delivery (sleep, reboot, SIGKILL) would lose every mark it had earned
    and re-send the lot. Cheap — one atomic replace under a lock we already hold.
    """
    url, token = _resolve_becos()
    ident = _becos_identity_headers()
    session_id = buf.get("session_id", "")
    sent = 0
    failed: list[tuple[dict, gbrain_client.GbrainError]] = []
    dead_letter_errors: list[gbrain_client.GbrainError] = []
    for ev in _build_events(buf):
        t, args, event_id = ev["turn"], ev["args"], ev["event_id"]
        seq = t.get("seq", 0)
        receipt = None
        try:
            result = gbrain_client.call_tool(
                url, token, "vonic_remember", args, timeout, extra_headers=ident,
            )
            receipt = gbrain_client.result_receipt(result, "vonic_remember")
            if receipt.present:
                if not receipt.valid:
                    raise _receipt_error(receipt)
                if receipt.acceptance == "accepted" and receipt.state == "pending":
                    t["uploaded"] = True  # durable ownership transferred; ingestion is not final
                    sent += 1
                    _log_delivery(session_id, seq, "turn", event_id,
                                  f"accepted:{receipt.condition}")
                    if checkpoint:
                        checkpoint()
                    continue
                if (receipt.acceptance == "accepted" and receipt.state == "terminal"
                        and receipt.outcome == "succeeded"):
                    t["uploaded"] = True
                    sent += 1
                    _log_delivery(session_id, seq, "turn", event_id,
                                  f"succeeded:{receipt.condition}")
                    if checkpoint:
                        checkpoint()
                    continue
                raise _receipt_error(receipt)
        except gbrain_client.GbrainError as exc:
            if _receipt_auth(receipt, str(exc)):
                # Auth is operational, not poison payload. Keep the event pending with its original
                # id and never advance the dead-letter budget.
                failed.append((t, gbrain_client.GbrainError(str(exc), transport=True)))
                _log_delivery(session_id, seq, "turn", event_id, f"auth deferred: {exc}")
                continue
            if receipt is not None:
                if receipt.retryable:
                    failed.append((t, gbrain_client.GbrainError(str(exc), transport=True)))
                    _log_delivery(session_id, seq, "turn", event_id,
                                  f"retryable deferred: condition={receipt.condition} {exc}")
                    continue
                # The backend has made a terminal payload verdict. Retrying the identical event is
                # pointless; preserve it immediately in the remediation ledger and unblock the queue.
                if _log_dead_letter(session_id, t, str(exc)):
                    t["uploaded"] = True
                    _log_delivery(session_id, seq, "turn", event_id,
                                  f"dead-lettered: condition={receipt.condition}")
                    if checkpoint:
                        checkpoint()
                else:
                    error = gbrain_client.GbrainError(
                        f"dead-letter persistence failed: condition={receipt.condition}",
                        transport=True,
                    )
                    dead_letter_errors.append(error)
                    _log_delivery(session_id, seq, "turn", event_id,
                                  "dead-letter write failed: retained")
                continue
            if _ALREADY_STORED in str(exc):
                # Compatibility for pre-receipt servers only. Structured conflict returns above and
                # is preserved as remediation data instead of being relabelled as success.
                t["uploaded"] = True
                _log_delivery(session_id, seq, "turn", event_id,
                              "legacy duplicate: already stored")
                continue
            t["attempts"] = t.get("attempts", 0) + 1
            failed.append((t, exc))
            _log_delivery(session_id, seq, "turn", event_id,
                          f"error {t['attempts']}/{_MAX_TURN_ATTEMPTS}: {exc}")
            continue
        t["uploaded"] = True
        sent += 1
        _log_delivery(session_id, seq, "turn", event_id, "ok")  # legacy server fallback
        if checkpoint:
            checkpoint()

    if failed and not sent and all(exc.transport for _t, exc in failed):
        for t, _exc in failed:
            if t.get("attempts"):
                t["attempts"] -= 1   # an unreachable endpoint must not spend any turn's budget
        raise failed[-1][1]      # defer the whole buffer to the next hook, as before
    for t, exc in failed:
        if t.get("attempts", 0) >= _MAX_TURN_ATTEMPTS:
            if _log_dead_letter(session_id, t, str(exc)):
                t["uploaded"] = True   # only the durable remediation copy permits local completion
                _log_delivery(session_id, t.get("seq", 0), "turn", "", "dead-lettered")
            else:
                dead_letter_errors.append(gbrain_client.GbrainError(
                    "dead-letter persistence failed: legacy poison turn retained", transport=True,
                ))
                _log_delivery(session_id, t.get("seq", 0), "turn", "",
                              "dead-letter write failed: retained")
    if dead_letter_errors:
        raise dead_letter_errors[-1]


class _ToolUnavailable(Exception):
    """The connector doesn't advertise the async enqueue tool — fall back to the sync path."""


# Substrings a server-answered "unknown tool" error carries. Only a NON-transport (server-answered)
# failure matching these triggers the sync fallback; a transport error is a live connectivity
# problem that must defer, not downgrade. Mirrors backfill.py, which imports these from here.
_UNAVAIL_HINTS = ("not found", "unknown tool", "no such tool", "not registered",
                  "-32601", "-32602", "unknown_tool")


def _async_deliver() -> bool:
    """Route live becos delivery through the async ingest queue (``becos_backfill_upload``) instead
    of a synchronous per-turn ``vonic_remember``. Async is the default; set
    ``VONIC_CAPTURE_ASYNC_DELIVER`` to any value other than ``"1"`` to force sync. Falls back to
    sync automatically when an older connector does not advertise the enqueue tool."""
    return os.environ.get("VONIC_CAPTURE_ASYNC_DELIVER", "1") == "1"


def _deliver_becos_async(buf: dict, timeout: float) -> None:
    """Enqueue this buffer's unsent turns onto the async ingest queue as ONE job, rather than a
    synchronous ``vonic_remember`` per turn. The server durably owns the turns on acceptance, so the
    client enqueues-and-returns — the ~100s distiller stays server-side (becos-dashboard#2).

    ``batch_id`` is PER-DELIVERY, keyed on the max seq delivered. The queue dedups jobs on
    ``(tenant, batch_id, session_id)`` and does NOT append to an existing job, so a stable
    per-session id would drop every turn after the first Stop hook. Max-seq is monotonic across a
    growing session (each hook → a new job) yet stable for a retried identical delivery (idempotent
    no-op — the job already exists and will drain). ``event_id`` (``session:seq``) dedups at the
    runtime as the backstop for any cross-job overlap.

    Reuses ``_build_events`` — the same payload the sync path and backfill upload build — so the two
    delivery paths stay byte-identical. Raises ``_ToolUnavailable`` when the connector doesn't
    advertise the enqueue tool, so the caller falls back to synchronous delivery (older connector).
    """
    events = _build_events(buf)
    if not events:
        return
    url, token = _resolve_becos()
    ident = _becos_identity_headers()
    session_id = buf.get("session_id", "")
    max_seq = max(e["turn"].get("seq", 0) for e in events)
    batch_id = f"live-{session_id}-{max_seq}"
    sessions = [{"session_id": session_id,
                 "turns": [{"event_id": e["event_id"], "args": e["args"]} for e in events]}]
    try:
        result = gbrain_client.call_tool(url, token, "becos_backfill_upload",
                                         {"sessions": sessions, "batch_id": batch_id},
                                         timeout, extra_headers=ident)
        status = _backfill_handoff(
            result, operation="becos_backfill_upload", batch_id=batch_id,
            session_id=session_id, turns=len(events),
        )
    except gbrain_client.GbrainError as exc:
        # A server-answered "unknown tool" means this connector predates the enqueue path → sync
        # fallback. A transport error is a live outage → re-raise so the buffer defers, as before.
        if not exc.transport and any(h in str(exc).lower() for h in _UNAVAIL_HINTS):
            raise _ToolUnavailable() from exc
        raise
    # Accepted and durably queued — the server owns them now. Mark uploaded so the buffer finalizes
    # and the next hook doesn't re-enqueue; a crash before this mark is safe (event_id dedups).
    for e in events:
        e["turn"]["uploaded"] = True
        _log_delivery(session_id, e["turn"].get("seq", 0), "turn", e["event_id"], status)


def _deliver_rest(buf: dict, url: str, token: str, timeout: float) -> None:
    """Custom-endpoint transport: one JSON POST per message (user, then assistant)."""
    session_id = buf.get("session_id", "")
    # On by default: sessionId is what gives the endpoint a real conversation key. Without
    # it every turn collapses into one server-derived conversation and the prompt->reply
    # pairing is lost. Set VONIC_LOG_LINK_FIELDS=0 for an endpoint that rejects them.
    link_fields = os.environ.get("VONIC_LOG_LINK_FIELDS", "1") != "0"
    cwd = buf.get("cwd", "")
    git = _is_git(cwd)
    repo = _repo_name(cwd) if git else None
    branch = _branch_or_none(cwd) if git else None
    commit = _full_commit(cwd) if git else None
    for t in buf.get("turns", []):
        seq = t.get("seq", 0)
        for role, tkey, sent, keyf in (
            ("user", "user", "user_sent", "user_key"),
            ("assistant", "assistant", "asst_sent", "asst_key"),
        ):
            if t.get(sent) or not t.get(tkey):
                continue
            text = _rest_text(t.get(tkey, ""))
            if not text:
                t[sent] = True  # nothing left after sanitizing
                continue
            if not t.get(keyf):
                t[keyf] = str(uuid.uuid4())  # stable Idempotency-Key, reused on retry
            key = t[keyf]
            payload = {
                "timestamp": t.get("ts") or _iso_now(),
                "role": role,
                "text": text,
            }
            # Every key here must exist in the receiving schema — see contracts/
            # coding-agent-message.json. Endpoints may set extra="forbid", in which case a
            # single unknown key rejects the WHOLE message, taking the valid fields with it.
            if link_fields:
                payload["sessionId"] = session_id  # links a prompt to its answer
                payload["turnIndex"] = seq
            if git:
                payload["repositoryName"] = repo
                payload["branch"] = branch
                payload["commitId"] = commit
            try:
                rest_client.post_message(url, token, payload, key, timeout)
            except rest_client.RestError as exc:
                _log_delivery(session_id, seq, role, key, f"error: {exc}")
                raise
            t[sent] = True  # only after a successful POST
            _log_delivery(session_id, seq, role, key, "200")


def _merge_marks(buffer_path: str, mem_buf: dict) -> None:
    """Persist delivery marks from an in-memory buffer onto the on-disk one under a BRIEF lock,
    matching turns by ``seq`` so a turn a concurrent ``_record_turns`` appended is never clobbered.

    This is what lets becos delivery run the (slow, prod) ``vonic_remember`` calls WITHOUT holding
    the buffer lock: the lock is taken only for these millisecond read-modify-writes, so a Stop
    hook's ``_record_turns`` never waits behind an in-flight — or backed-up — delivery. Because
    ``_record_turns`` only ever appends turns and never touches an existing turn's
    ``uploaded``/``attempts``, copying just those two fields by ``seq`` is a lossless merge in both
    directions (record's appends survive; deliver's marks survive).
    """
    with _locked(buffer_path):
        disk = _load_buffer(buffer_path)
        if not disk:
            return  # buffer finalized/removed between calls — nothing to persist
        by_seq = {t.get("seq"): t for t in disk.get("turns", [])}
        for t in mem_buf.get("turns", []):
            d = by_seq.get(t.get("seq"))
            if d is None:
                continue
            if t.get("uploaded"):
                d["uploaded"] = True
            if t.get("attempts"):
                d["attempts"] = max(d.get("attempts", 0), t.get("attempts", 0))
        _save_buffer(buffer_path, disk)


def _finalize_if_done(buffer_path: str) -> None:
    """Remove a fully-delivered, ``done`` buffer under a brief lock. Keeps the ``.lock`` file."""
    with _locked(buffer_path):
        buf = _load_buffer(buffer_path)
        if not buf or not buf.get("done"):
            return
        if all(t.get("uploaded") for t in buf.get("turns", [])):
            # The buffer goes; the `.lock` file MUST NOT. Unlinking a held flock does not release
            # waiters — it only detaches the name; the next deliverer would `open(...".lock")` a NEW
            # inode and lock that, letting two processes deliver the same buffer concurrently.
            # `sweep` prunes orphan locks.
            try:
                os.remove(buffer_path)
            except OSError:
                pass


def _deliver(buffer_path: str) -> int:
    if not os.path.exists(buffer_path):
        return 0
    becos_url = os.environ.get("VONIC_BECOS_URL", "").strip()  # set → becos tools on vonic_agent
    log_url = os.environ.get("VONIC_LOG_URL", "").strip()      # set → custom REST endpoint
    if not becos_url and not log_url and not os.environ.get("VONIC_GBRAIN_URL", "").strip():
        # Nothing configured: fall back to the hosted becos endpoint rather than doing
        # nothing at all. Guarded so an explicit rest/gbrain install keeps its transport.
        becos_url = _resolve_becos_url()
    # 120s, not 15s: a `vonic_remember` fact is ingested AND processed server-side before it
    # answers — measured 20-50s per turn. Delivery is detached (`_dispatch`), so waiting costs the
    # session nothing; `backfill.py` has used 240 all along.
    timeout = float(_env_int("VONIC_CODECOLLAB_TIMEOUT", 120))

    if becos_url:
        # RECORD/DELIVER LOCK SPLIT — never hold the buffer lock across the network. Load + build
        # under a brief lock, run `_deliver_becos` lock-free (its `checkpoint` persists each mark
        # under its own brief, merge-based lock), then merge post-loop marks and finalize. A slow
        # or backed-up prod delivery no longer blocks the next Stop hook's `_record_turns`.
        with _locked(buffer_path):
            buf = _load_buffer(buffer_path)
            if not buf:
                return 0
            _check_build(buf, buf.get("session_id", ""))
            # A `done` buffer must still finalize (cleanup) even when every turn is already uploaded.
            if not any(not t.get("uploaded") for t in buf.get("turns", [])) and not buf.get("done"):
                return 0
            _save_buffer(buffer_path, buf)  # persist `_check_build` info before releasing the lock
        try:
            if _async_deliver():
                try:
                    _deliver_becos_async(buf, timeout)
                except _ToolUnavailable:
                    _debug("async enqueue tool unavailable; falling back to sync becos delivery")
                    _deliver_becos(buf, timeout, checkpoint=lambda: _merge_marks(buffer_path, buf))
            else:
                _deliver_becos(buf, timeout, checkpoint=lambda: _merge_marks(buffer_path, buf))
            _merge_marks(buffer_path, buf)  # persist post-loop marks (drops, attempts, dead-letters)
            _debug(f"delivered via becos ({len(buf.get('turns', []))} turns)")
            _finalize_if_done(buffer_path)
        except (gbrain_client.GbrainError, rest_client.RestError,
                urllib.error.URLError, OSError, KeyError) as exc:
            _merge_marks(buffer_path, buf)  # keep any marks earned before the outage, then defer
            _deferred(exc)
        return 0

    # rest / mcp transports: unchanged whole-delivery lock (fast/local; not the prod-latency path).
    rest = bool(log_url)
    with _locked(buffer_path):
        buf = _load_buffer(buffer_path)
        _check_build(buf, buf.get("session_id", ""))
        turns = buf.get("turns", [])
        pending = (any(not _turn_sent(t) for t in turns) if rest
                   else any(not t.get("uploaded") for t in turns))
        # A `done` buffer must still deliver once (event="end" → server-side finalize/distil) and
        # then get cleaned up, even when every turn was already uploaded by the last turn's delivery.
        if not pending and not buf.get("done"):
            return 0
        try:
            if log_url:
                _deliver_rest(buf, log_url, os.environ.get("VONIC_LOG_TOKEN", "").strip(), timeout)
                transport, complete = "rest", all(_turn_sent(t) for t in turns)
            else:
                _deliver_mcp(buf, timeout)
                transport, complete = "mcp", all(t.get("uploaded") for t in turns)
            _save_buffer(buffer_path, buf)
            _debug(f"delivered via {transport} ({len(turns)} turns)")
            if buf.get("done") and complete:
                try:
                    os.remove(buffer_path)
                except OSError:
                    pass
        except (gbrain_client.GbrainError, rest_client.RestError,
                urllib.error.URLError, OSError, KeyError) as exc:
            _save_buffer(buffer_path, buf)  # keep un-sent messages (+ their keys) for the next sweep
            _deferred(exc)
    return 0


def _deferred(exc: Exception) -> None:
    """Report a deferred delivery at the right volume.

    Transient (server down, timeout, 503) stays on _debug: it resolves itself, and warning
    every turn would train the user to ignore the channel. Configuration stays on _warn: it
    will never resolve itself, and silence here is what let three setup gaps look identical
    to "working"."""
    if getattr(exc, "config", False):
        _warn(f"delivery not configured — {exc}")
    else:
        _debug(f"delivery deferred: {exc}")


def _dispatch(buffer_path: str) -> None:
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--deliver", buffer_path],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, env=os.environ.copy(),
        )
    except OSError as exc:
        _debug(f"could not spawn deliverer: {exc}")


# ── hook modes ───────────────────────────────────────────────────────────────

def _repo_activity(fn):
    """Run a repo_activity call, swallowing everything and returning None on failure.

    Imported lazily so `capture` stays the leaf module every other script imports (repo_activity
    imports *it*), and so a vendored copy shipped without the file still runs.
    """
    try:
        import repo_activity  # noqa: PLC0415 — deliberate: lazy, optional, no import cycle
        return fn(repo_activity)
    except Exception as exc:  # noqa: BLE001 — a report must never break the session
        _debug(f"repo-activity: {exc}")
        return None


def _emit_repo_report(text: str | None) -> None:
    """Surface the per-turn repository report. Silent when the turn touched no checkout, so a
    pure-conversation turn stays clean.

    Ephemeral by nature: Claude Code renders `systemMessage` as an attachment that reaches neither
    the transcript nor the model, which is why `_log_repo_activity` keeps the durable copy.
    """
    if text:
        print(json.dumps({"systemMessage": text}))


def _record_turns(event: dict, done: bool, repos: dict | None = None) -> str | None:
    session_id = event.get("session_id", "")
    cwd = event.get("cwd") or os.getcwd()
    path = _buffer_path(session_id)
    turns = _parse_session(event.get("transcript_path", ""))
    # Scope each turn by the directory IT ran in, not one cwd for the whole session. `git`
    # subprocesses stay outside the lock and cost one call per DISTINCT cwd (cached), not per
    # turn — a mixed session has a handful of directories, not hundreds. A turn with no recorded
    # cwd (older transcripts, or a line that carried none) falls back to the event cwd, so live
    # capture and single-directory sessions are unchanged.
    ident_by_cwd = {c: _git_identity(c) for c in {t.get("cwd") or cwd for t in turns}}
    with _locked(path):
        buf = _load_buffer(path) or _new_buffer(session_id, cwd)
        _check_build(buf, session_id)
        seen = buf.get("turns_recorded", 0)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for i, t in enumerate(turns[seen:]):
            if not (t.get("user") or t.get("assistant")):
                continue
            # `seq` (global turn index) becomes the entry's `source`, keeping the
            # dedup key (page, date, summary, source) unique — otherwise turns
            # whose prompts share an opening (e.g. repeated "yes") would MERGE.
            buf["turns"].append({
                "seq": seen + i,
                "date": today,
                "ts": _iso_now(),
                "git": ident_by_cwd[t.get("cwd") or cwd],   # this turn's own scope; frozen here
                "user": _clean(t.get("user", "")),
                "assistant": _clean(t.get("assistant", "")),
                "files": t.get("files", []),
                # opt-in, default OFF; redacted + capped, and NEVER folded into `content`
                "thinking": (_cap(_redact(t.get("thinking", "")), _env_int("VONIC_MAX_THINKING", 20_000))
                             if _capture_thinking() else ""),
                "code": _prep_code(t.get("code", []), t.get("assistant", "")) if _capture_code() else [],
                "uploaded": False,
            })
        # One `turn` run can record several turns (a resume replays the backlog), but the report
        # covers activity since the LAST UserPromptSubmit — so it belongs to the last turn only.
        # It has to be in the buffer before `_dispatch`, which delivers from a separate process.
        if repos and buf["turns"]:
            buf["turns"][-1]["repos"] = repos
        buf["turns_recorded"] = len(turns)
        if done:
            buf["done"] = True
        _save_buffer(path, buf)
        has_pending = any(not t.get("uploaded") for t in buf["turns"])
    # Dispatch when finalizing (done) too, so the last turn's already-uploaded buffer still gets a
    # final event="end" delivery + cleanup instead of lingering forever.
    return path if (has_pending or done) else None


def _record_session(session: dict, done: bool) -> str | None:
    """Record turns from a PRE-NORMALIZED session (non-Claude-Code adapters, e.g. Opencode).

    Same buffer / dedup / delivery path as the Claude Code hook, but the turns are supplied by
    the caller's adapter instead of parsed from a Claude Code transcript. ``session`` shape::

        {session_id, cwd, turns: [{seq?, ts?, user, assistant, files?, accesses?}], branch?, commit?,
         client_tag?}

    Send the WHOLE session each turn (idempotent, overwrite-by-slug); already-recorded turns are
    skipped by ``turns_recorded``. ``user``/``assistant`` are cleaned here (fenced-code strip) so
    the generic privacy rule stays in one place — the adapter only drops non-text parts upstream.
    """
    session_id = session.get("session_id", "")
    cwd = session.get("cwd") or os.getcwd()
    tag = session.get("client_tag") or os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc")
    incoming = session.get("turns", []) or []
    path = _buffer_path(session_id)
    git = _git_identity(cwd)
    # The adapter has already reduced tool inputs to path + access mode. Resolve those local paths
    # before taking the buffer lock; only repository slug/branch/access can reach wire metadata.
    repo_activity = _repo_activity(lambda mod: mod.resolve_turn_accesses(incoming, cwd))
    if not isinstance(repo_activity, list) or len(repo_activity) != len(incoming):
        repo_activity = [None] * len(incoming)
    if git and session.get("branch"):
        git["branch"] = session["branch"]   # the adapter knows its own checkout better than cwd does
    with _locked(path):
        buf = _load_buffer(path) or _new_buffer(session_id, cwd, tag)
        if session.get("branch") is not None:
            buf["branch"] = session["branch"]
        if session.get("commit") is not None:
            buf["commit"] = session["commit"]
        seen = buf.get("turns_recorded", 0)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        for i, t in enumerate(incoming[seen:]):
            user = _clean(t.get("user", "") or "")
            assistant = _clean(t.get("assistant", "") or "")
            if not (user or assistant):
                continue
            entry = {
                "seq": t.get("seq", seen + i),
                "date": today,
                "ts": t.get("ts") or _iso_now(),
                "git": git,
                "user": user,
                "assistant": assistant,
                "files": t.get("files", []) or [],
                "uploaded": False,
            }
            activity = repo_activity[seen + i]
            # Adapters that already know the turn's repository activity — the Codex plugin builds it
            # from the typed `patch_apply_end` event via repo_activity.for_paths — pass a `repos`
            # dict ({repositories, unresolved_paths}); carry it so `_attach_repo_activity` ships
            # `repositories_touched`. Otherwise use the path-only activity resolved by this adapter.
            if t.get("repos"):
                entry["repos"] = t["repos"]
            elif activity:
                entry["repos"] = activity
            buf["turns"].append(entry)
        buf["turns_recorded"] = len(incoming)
        if done:
            buf["done"] = True
        _save_buffer(path, buf)
        has_pending = any(not t.get("uploaded") for t in buf["turns"])
    # Dispatch when finalizing (done) too, so the last turn's already-uploaded buffer still gets a
    # final event="end" delivery + cleanup instead of lingering forever.
    return path if (has_pending or done) else None


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""

    if mode == "--deliver":
        return _deliver(sys.argv[2]) if len(sys.argv) > 2 else 0

    if os.environ.get("VONIC_CODECOLLAB_DISABLED") == "1":
        return 0

    if mode == "sweep":
        # SessionStart: retry any buffers left with pending turns, then (once per
        # machine) surface a backfill offer for pre-existing session history.
        _migrate_legacy_buffers()   # one-time: relocate shared-cache buffers into per-runtime dirs
        for path in glob.glob(os.path.join(_sessions_dir(), "*.json")):
            _dispatch(path)
        _prune_orphan_locks()
        _first_run_notice()
        _repo_activity(lambda mod: mod.prune_markers())
        return 0

    if mode == "deliver-session":
        # Non-Claude-Code adapters (e.g. the Opencode plugin) pipe a PRE-NORMALIZED session
        # (see _record_session) on stdin. Optional 2nd arg: event "turn" (default) | "finalize".
        event_kind = sys.argv[2] if len(sys.argv) > 2 else "turn"
        try:
            session = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError) as exc:
            _debug(f"bad deliver-session input: {exc}")
            return 0
        path = _record_session(session, done=(event_kind == "finalize"))
        if path:
            _dispatch(path)
        return 0

    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError) as exc:
        _debug(f"bad hook input: {exc}")
        return 0

    if mode in ("turn", "finalize"):
        # Which checkouts this turn read/wrote. Collected BEFORE recording, because the turn it
        # belongs to must carry it into the buffer before `_dispatch` hands delivery to a separate
        # process. Only `turn` collects: `finalize` has no turn of its own and no marker to read.
        activity = _repo_activity(lambda mod: mod.collect(event)) if mode == "turn" else None
        path = _record_turns(event, done=(mode == "finalize"), repos=activity)
        if path:
            _dispatch(path)
        if mode == "turn":
            _emit_repo_report(_repo_activity(lambda mod: mod.render(activity)))
            _log_repo_activity(event.get("session_id", ""), activity)
        return 0

    _debug(f"unknown mode: {mode!r}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — a logging hook must never break the session
        _debug(f"unexpected: {exc}")
        sys.exit(0)
