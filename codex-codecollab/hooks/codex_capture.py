#!/usr/bin/env python3
"""Codex capture adapter (client tag `cx`).

Codex runs this as a **hook command** (config in ~/.codex/hooks.json). It reads the hook's JSON
payload on stdin, keyed by `hook_event_name`, and turns each completed Q&A into a codecollab turn
that it hands to the **vendored** delivery core (`deliver/capture.py deliver-session`) — the same
entrypoint the Opencode port uses. This adapter never reimplements privacy/auth/dedup/delivery.

Why we build turns from the hook payload (not the transcript):
  Codex hook stdin gives us the two stable, documented fields we need —
    * UserPromptSubmit -> {"prompt": "..."}          (the user's text)
    * Stop             -> {"last_assistant_message": "..."}  (the assistant's text reply)
  We pair them per session. We read these two fields because they are the *simplest stable source*
  for the core turn text: the `transcript_path` rollout.jsonl "isn't a stable interface for hooks and
  may change over time" (Codex docs). A tested *consequence* of only reading these two fields is that
  reasoning / command output / tool I/O never leave the machine (asserted in test/test_capture_cx.py)
  — that is a real guarantee, but it is a byproduct of the sourcing choice, not the reason for it, and
  it is not an absolute project invariant: file-touch enrichment (`changed_files` / repo activity)
  reads additional signals on purpose. Those enrichment paths must keep the same privacy bar (paths
  and repo slugs only, never file contents/diffs).

Events -> action:
  SessionStart      -> sweep  (retry any buffers a prior session left undelivered)
  UserPromptSubmit  -> stash the prompt as this session's pending user turn (recall is a *separate*
                       hook: deliver/recall.py, wired in hooks.json)
  Stop              -> complete the turn with last_assistant_message; deliver the whole session
  SessionEnd        -> deliver + finalize (mark the session done)

Fail-open: any error exits 0 so Codex is never blocked. Set VONIC_CODECOLLAB_DEBUG=1 to log.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DELIVER = HERE.parent / "deliver" / "capture.py"
CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "codecollab"
PENDING_DIR = CACHE / "cx-pending"          # per-session accumulated turns (survives between hooks)
LOG = CACHE / "cx-plugin.log"
CLIENT_TAG = os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cx")


def _debug(msg: str) -> None:
    if os.environ.get("VONIC_CODECOLLAB_DEBUG") != "1":
        return
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _pending_path(session_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "session"
    return PENDING_DIR / f"{safe}.json"


def _load_pending(session_id: str) -> dict:
    try:
        return json.loads(_pending_path(session_id).read_text("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_pending(session_id: str, state: dict) -> None:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    _pending_path(session_id).write_text(json.dumps(state), "utf-8")


def _spawn_detached(args: list[str], stdin_text: str | None = None) -> None:
    """Fire-and-forget the vendored core so no hook blocks on the network.

    Hooks have tight budgets (SessionEnd: max 3s). Delivery is buffered locally first and retried by
    the SessionStart sweep, so it is always safe to detach and not wait.
    """
    try:
        p = subprocess.Popen(  # noqa: S603
            args,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        if stdin_text is not None:
            try:
                p.stdin.write(stdin_text.encode())
                p.stdin.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — never block Codex
        _debug(f"spawn failed: {exc}")


def _deliver_session(session: dict, event_kind: str) -> None:
    """Pipe a pre-normalized session to the vendored `capture.py deliver-session` (detached)."""
    _spawn_detached([sys.executable, str(DELIVER), "deliver-session", event_kind],
                    stdin_text=json.dumps(session))


# `patch_apply_end` change kind -> the vendored core's file-op vocabulary (repo_activity._OP_RANK).
_OP_FOR_KIND = {"add": "created", "delete": "deleted", "update": "modified"}


def _changed_paths(transcript_path: str, offset: int) -> tuple[list[str], int]:
    """Absolute paths this turn WROTE since `offset`, plus the new offset (see `_changed_ops`)."""
    ops, new_offset = _changed_ops(transcript_path, offset)
    return [op["path"] for op in ops], new_offset


def _changed_ops(transcript_path: str, offset: int) -> tuple[list[dict], int]:
    """File ops (``{"path", "op"}``) this turn WROTE, read from the rollout since `offset`; plus the
    new offset.

    The ONE signal we take from the rollout. A file edit lands as an ``event_msg`` whose payload is
    a ``patch_apply_end`` carrying ``changes`` — a map keyed by absolute path. We take its KEYS (the
    paths) and each change's ``type`` string (add / update / delete); the ``content`` value (the file
    body / diff) is never read, so the privacy bar is the same as the rest of capture: paths, never
    contents. Defensive + fail-closed — an unreadable file or an unexpected shape yields no path
    rather than a wrong or leaky one. A torn final line (a write in flight) is left unconsumed so the
    next turn re-reads it instead of losing it.
    """
    if not transcript_path:
        return [], offset
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(offset)
            raw = fh.readlines()
            new_offset = fh.tell()
    except OSError as exc:  # noqa: BLE001
        _debug(f"rollout read failed: {exc}")
        return [], offset
    if raw and not raw[-1].endswith(b"\n"):   # partial trailing write — re-read it next time
        new_offset -= len(raw[-1])
        raw = raw[:-1]
    ops: list[dict] = []
    seen: set[str] = set()
    for line in raw:
        try:
            rec = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # a torn or non-UTF8 line — skip, never guess
        if not isinstance(rec, dict):
            continue
        payload = rec.get("payload")
        if not (isinstance(payload, dict) and payload.get("type") == "patch_apply_end"):
            continue
        if payload.get("success") is False:
            continue  # a patch that failed to apply changed nothing
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            continue
        for path, change in changes.items():
            if isinstance(path, str) and path and path not in seen:
                seen.add(path)
                kind = change.get("type") if isinstance(change, dict) else None
                ops.append({"path": path, "op": _OP_FOR_KIND.get(kind, "modified")})
    return ops, new_offset


def _repo_activity(paths: list[str], cwd: str) -> dict | None:
    """Repository activity ({repositories, unresolved_paths}) for the turn's written paths, built by
    the shared resolver so the slug/branch identity matches ``repo_name``. Lazily imports the
    vendored ``repo_activity`` (a rollout with no edit pays nothing). Fail-open: never raises."""
    if not paths:
        return None
    try:
        deliver_dir = str(DELIVER.parent)
        if deliver_dir not in sys.path:
            sys.path.insert(0, deliver_dir)
        import repo_activity  # noqa: PLC0415 — lazy: only when a turn wrote files
        return repo_activity.for_paths(paths, cwd)
    except Exception as exc:  # noqa: BLE001 — enrichment must never break capture
        _debug(f"repo-activity failed: {exc}")
        return None


def _turn_activity(payload: dict, state: dict,
                   cwd: str) -> tuple[list[str], dict | None, list[dict]]:
    """The current turn's changed files, repository activity and file ops, advancing the
    per-session rollout offset stored in ``state``. Codex opens a NEW rollout on resume, so the
    offset is reset whenever the transcript path moves. Mutates ``state`` (the caller persists it);
    fail-open."""
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return [], None, []
    if state.get("rollout_path") != transcript:     # new rollout file — start from its beginning
        state["rollout_path"] = transcript
        state["rollout_offset"] = 0
    ops, new_offset = _changed_ops(transcript, state.get("rollout_offset", 0))
    state["rollout_offset"] = new_offset
    paths = [op["path"] for op in ops]
    return paths, _repo_activity(paths, state.get("cwd", cwd)), ops


def _sweep() -> None:
    """SessionStart: let the vendored core retry any undelivered buffers (detached)."""
    _spawn_detached([sys.executable, str(DELIVER), "sweep"])


def _ensure_backfill_skill() -> None:
    """SessionStart: make sure the `/backfill` and `/login` skills are installed (idempotent,
    fail-open)."""
    try:
        from skill_install import ensure_backfill_skill, ensure_login_skill
        ensure_backfill_skill(HERE.parent)
        ensure_login_skill(HERE.parent)
    except Exception as exc:  # noqa: BLE001 — never block Codex
        _debug(f"skill install skipped: {exc}")


def main() -> int:
    if os.environ.get("VONIC_CODECOLLAB_DISABLED") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        _debug(f"bad hook input: {exc}")
        return 0

    event = payload.get("hook_event_name", "")
    session_id = payload.get("session_id") or payload.get("thread_id") or ""
    cwd = payload.get("cwd") or os.getcwd()

    if event == "SessionStart":
        _sweep()
        _ensure_backfill_skill()   # one-step install of the /backfill skill (idempotent, fail-open)
        return 0

    if event == "UserPromptSubmit":
        prompt = (payload.get("prompt") or "").strip()
        if prompt and not prompt.startswith("/"):
            state = _load_pending(session_id)
            state.setdefault("cwd", cwd)
            state["pending_user"] = prompt
            _save_pending(session_id, state)
            _debug(f"prompt stashed  session={session_id[:8]}  {prompt[:60]!r}")
        return 0

    if event in ("Stop", "SessionEnd"):
        state = _load_pending(session_id)
        turns = state.get("turns", [])
        assistant = (payload.get("last_assistant_message") or "").strip()
        user = state.pop("pending_user", "")
        if event == "Stop" and (user or assistant):
            files, repos, file_ops = _turn_activity(payload, state, cwd)
            turn = {"seq": len(turns), "ts": _iso_now(),
                    "user": user, "assistant": assistant, "files": files}
            if file_ops:
                # The vendored core builds `changed_files` from `file_ops` (resolved to repo-relative
                # paths locally); the absolute `files` list alone no longer reaches the wire.
                turn["file_ops"] = file_ops
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                # Codex's own turn id, which the rollout's `task_complete` also carries: the vendored
                # core hashes it into `<session>:cx-v2:<digest>`, so live capture and backfill of the
                # same turn land on ONE event id. Without it the core falls back to hashing the
                # wall-clock `ts` above, which backfill can never reproduce.
                turn["source_message_ids"] = [turn_id]
            if repos:
                turn["repos"] = repos   # carried through _record_session -> repositories_touched
            turns.append(turn)
            state["turns"] = turns
            _save_pending(session_id, state)
        if turns:
            session = {"session_id": session_id, "cwd": state.get("cwd", cwd),
                       "client_tag": CLIENT_TAG, "turns": turns}
            _deliver_session(session, "finalize" if event == "SessionEnd" else "turn")
            _debug(f"delivered {len(turns)} turns  session={session_id[:8]}  event={event}")
        if event == "SessionEnd":
            try:
                _pending_path(session_id).unlink()
            except Exception:  # noqa: BLE001
                pass
        return 0

    _debug(f"ignored event {event!r}")
    return 0


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main())
