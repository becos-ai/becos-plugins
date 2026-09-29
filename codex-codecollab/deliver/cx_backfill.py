"""One-off backfill of existing Codex sessions into memory (client tag `cx`).

codecollab captures Codex sessions going forward (the hooks in ``hooks/codex_capture.py`` ship each
completed turn live). This sweeps sessions that happened BEFORE the plugin was installed — or any
you will not reopen — so their turns land in memory too, through the same
``_build_events -> becos_backfill_upload`` path the Opencode and Claude Code ports use.

Source + the privacy boundary
------------------------------
Codex persists each session to ``~/.codex/sessions/**/rollout-*.jsonl``. That file has TWO layers:

  * a **raw model stream** (``response_item`` records: roles user/assistant/developer, plus
    ``reasoning`` and ``custom_tool_call*`` records) — carries injected context, chain-of-thought,
    patches and tool I/O, and
  * a **clean UI layer** (``event_msg`` records: ``user_message`` and ``task_complete``) — the exact
    same two fields the live hook captures: what the user typed and the final assistant reply.

This parser reads **only the clean UI layer**. It never opens ``response_item`` / ``reasoning`` /
``custom_tool_call*``. So backfill delivers the identical content live capture would, and the code
that could leak reasoning/patches/tool-output still never runs. If a rollout file does not present
the clean layer (an unrecognised or future format), it is **skipped, not best-effort parsed**
(fail-closed) — see ``_parse_rollout``. This is the decision recorded in issue #2 (option A, via the
clean event_msg layer).

Safe by default: a DRY RUN just lists what it would ingest (per project → session → turns). Pass
``--deliver`` to actually ship (needs the same env as live delivery — ``VONIC_BECOS_URL`` +
``VONIC_BECOS_TOKEN_CMD``/identity). Idempotent + resumable: async uploads land in a STABLE campaign
batch, a local manifest skips handed-off sessions, and the server dedups the rest (each turn dedups
by event_id).

    python3 cx_backfill.py                       # dry run: per-project sessions + turn counts
    python3 cx_backfill.py --project becos       # filter by repo (substring)
    python3 cx_backfill.py --session 019ff687     # narrow to a session (id or prefix; repeatable)
    python3 cx_backfill.py --deliver --async         # upload + hand off to the server-side worker
    python3 cx_backfill.py --deliver --async --watch # upload, then poll status to completion
    python3 cx_backfill.py --deliver --async --campaign myrun   # name the campaign (batch id)
"""

from __future__ import annotations

import glob
import json
import os
import socket
import sys
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402  (vendored core — reuse its event build + delivery + auth)
import gbrain_client  # noqa: E402  (vendored — MCP client for the connector's backfill tools)


def _sessions_root() -> str:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(home, "sessions")


# ── args ────────────────────────────────────────────────────────────────────
def _arg_value(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        return sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    return None


def _arg_values(flag: str) -> list[str]:
    """All values for a repeatable flag: ``--session A --session B,C`` -> ``[A, B, C]``."""
    out: list[str] = []
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            out.extend(v.strip() for v in sys.argv[i + 1].split(",") if v.strip())
    return out


# ── discovery (clean event_msg layer only; fail-closed) ──────────────────────
def _sid_from_path(path: str) -> str:
    """Best-effort session id from a rollout filename (used only when the file is unparseable).

    ``rollout-2026-08-12T19-11-18-019ff687-1f8b-7921-9d89-0b8439918c6f.jsonl`` -> the trailing UUID.
    """
    stem = os.path.basename(path)
    if stem.endswith(".jsonl"):
        stem = stem[:-6]
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


def _parse_rollout(path: str) -> dict | None:
    """Normalise ONE rollout file from its clean ``event_msg`` layer, or return ``None``.

    Returns ``{session_id, cwd, branch, commit, client_tag, turns:[{seq,user,assistant,ts,files}]}``.
    Turns are paired exactly as the live hook does: a ``user_message`` stashes the prompt, the next
    ``task_complete`` closes it with ``last_assistant_message``.

    Fail-closed: returns ``None`` when the file is empty, has no ``session_meta``, or carries no clean
    ``event_msg`` layer at all (an unrecognised/future format). We deliberately do NOT fall back to
    the raw ``response_item`` stream — that is the whole privacy boundary. A caller reads ``None`` as
    "skip this file, do not ingest".
    """
    session_id = cwd = branch = commit = None
    turns: list[dict] = []
    pending_user: str | None = None
    pending_user_ts: str | None = None
    seq = 0
    saw_clean = False
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                kind = rec.get("type")
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                if kind == "session_meta":
                    session_id = session_id or payload.get("session_id") or payload.get("id")
                    cwd = cwd or payload.get("cwd")
                    git = payload.get("git")
                    if isinstance(git, dict):
                        branch = branch or git.get("branch")
                        commit = commit or git.get("commit_hash") or git.get("commit")
                    continue
                if kind != "event_msg":
                    continue  # never read response_item / reasoning / tool records
                ptype = payload.get("type")
                if ptype == "user_message":
                    saw_clean = True
                    pending_user = (payload.get("message") or "").strip()
                    pending_user_ts = rec.get("timestamp")
                elif ptype == "task_complete":
                    saw_clean = True
                    assistant = (payload.get("last_agent_message") or "").strip()
                    user = (pending_user or "").strip()
                    # Real per-turn time from the rollout (each record carries a top-level ISO-8601
                    # `timestamp`). Using it — not wall-clock — makes `occurred_at` a stable function
                    # of the turn, so a re-backfill rebuilds byte-identical events (idempotent by
                    # value) instead of a fresh timestamp each run. See DECISIONS.md (re-upload dedup).
                    ts = rec.get("timestamp") or pending_user_ts
                    pending_user = None
                    pending_user_ts = None
                    if user or assistant:
                        turn = {"seq": seq, "user": user, "assistant": assistant,
                                "ts": ts, "files": []}
                        turn_id = payload.get("turn_id")
                        if isinstance(turn_id, str) and turn_id:
                            # The same id the live Stop hook reports, so both paths derive one
                            # `cx-v2` event id for this turn (see _buffer_for).
                            turn["source_message_ids"] = [turn_id]
                        turns.append(turn)
                        seq += 1
    except OSError:
        return None
    if not session_id or not saw_clean:
        return None  # unrecognised format → fail-closed skip
    return {"session_id": session_id, "cwd": cwd or "", "branch": branch, "commit": commit,
            "client_tag": os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cx"), "turns": turns,
            "path": path}


def _project_for(cwd: str) -> str:
    """Group label for the dry-run table: the repo name (like Claude Code's project slug)."""
    if not cwd:
        return "(no cwd)"
    return capture._repo_name(cwd) or os.path.basename(cwd.rstrip("/")) or "(no cwd)"


def discover() -> list[dict]:
    # Parse every rollout file, then MERGE files that share a session_id. Codex writes a NEW rollout
    # file on resume (new filename UUID) but keeps the ORIGINAL session_id in session_meta, so one
    # session can span several files. Merging them — in chronological (filename-sorted) order, with a
    # globally-unique seq across the merge — keeps the table one-row-per-session and, critically,
    # delivers every turn under a UNIQUE event_id: per-file seq restarts at 0, so unmerged files
    # would collide on "<session_id>:0" (and the manifest, keyed by session_id, would skip all but
    # the first file, silently dropping the rest). See issue #3.
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    skipped_rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(_sessions_root(), "**", "rollout-*.jsonl"),
                                 recursive=True)):
        sess = _parse_rollout(path)
        if sess is None:
            skipped_rows.append({"project": "(unrecognized)", "session": _sid_from_path(path),
                                 "path": path, "turns": 0, "cwd": "", "skipped": True,
                                 "scoped": False, "files": 1, "_session": None})
            continue
        groups.setdefault(sess["session_id"], []).append(sess)

    rows: list[dict] = []
    for sid, parts in groups.items():
        first = parts[0]  # parts are in sorted-path (chronological) order
        merged: list[dict] = []
        for part in parts:
            for t in part["turns"]:
                merged.append({**t, "seq": len(merged)})  # globally-unique seq across the merge
        turns = [t for t in merged if t.get("user") or t.get("assistant")]
        # Repository memory only ingests repo-scoped work, and `_build_events` drops turns whose cwd
        # is outside any git repo. Predict that here (Codex desktop runs each chat in a non-repo
        # scratch dir under ~/Documents/Codex/…) so the dry run's counts match what will land.
        session = {"session_id": sid, "cwd": first["cwd"], "branch": first["branch"],
                   "commit": first["commit"], "client_tag": first["client_tag"], "turns": merged}
        rows.append({"project": _project_for(first["cwd"]), "session": sid, "path": first["path"],
                     "turns": len(turns), "cwd": first["cwd"], "skipped": False,
                     "scoped": capture._is_git(first["cwd"]), "files": len(parts),
                     "_session": session})
    return rows + skipped_rows


# ── delivery (async upload, in-memory build; oc parity) ──────────────────────
class _ToolUnavailable(Exception):
    """The connector doesn't advertise the async backfill tools — fall back to the sync drain."""


# Substrings an MCP "unknown tool" error tends to carry. Only a server-answered (non-transport)
# failure matching these triggers the fallback; a transport error is a live connectivity problem.
_UNAVAIL_HINTS = ("not found", "unknown tool", "no such tool", "not registered",
                  "-32601", "-32602", "unknown_tool")


def _tool_data(result: dict) -> dict:
    """The tool's JSON payload out of an MCP result (structured content, else the text block)."""
    sc = result.get("structuredContent")
    if isinstance(sc, dict):
        if any(k in sc for k in ("rollup", "jobs", "batch_id")):
            return sc
        inner = sc.get("result")
        return inner if isinstance(inner, dict) else sc
    for block in result.get("content", []) or []:
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
    return {}


def _default_batch_id() -> str:
    """A STABLE batch id so re-runs land in the same campaign (cumulative status, idempotent
    enqueue). Override with --campaign to run a distinct campaign."""
    return _arg_value("--campaign") or f"backfill-{socket.gethostname().split('.')[0]}"


def _manifest_path() -> str:
    return os.path.join(capture._cache_dir(), "cx-backfill-manifest.jsonl")


def _uploaded_keys() -> set:
    keys = set()
    try:
        with open(_manifest_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                keys.add((r.get("batch_id"), r.get("session_id")))
    except OSError:
        pass
    return keys


def _record_manifest(batch_id: str, session_id: str, turns: int) -> None:
    rec = {"batch_id": batch_id, "session_id": session_id, "turns": turns, "ts": capture._iso_now()}
    try:
        os.makedirs(capture._cache_dir(), exist_ok=True)
        with open(_manifest_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _fresh_buffer(session: dict) -> dict:
    """An IN-MEMORY buffer for one normalised session — the shape ``_build_events`` consumes, but
    never read from / written to ``_buffer_path(session_id)``.

    Backfill must NOT share the live-capture buffer: a session also captured live has its turns
    already marked ``uploaded`` there, so reusing it would yield a spurious "empty". Building fresh
    means backfill always rebuilds every turn (the server dedups by event_id, so re-sending a
    live-delivered turn is a harmless duplicate, not double memory)."""
    cwd = session.get("cwd") or os.getcwd()
    tag = session.get("client_tag") or os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cx")
    buf = capture._new_buffer(session.get("session_id", ""), cwd, tag)
    git = capture._git_identity(cwd)
    if git and session.get("branch"):
        git["branch"] = session["branch"]
    if session.get("branch") is not None:
        buf["branch"] = session["branch"]
    if session.get("commit") is not None:
        buf["commit"] = session["commit"]
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for i, t in enumerate(session.get("turns", []) or []):
        user = capture._clean(t.get("user", "") or "")
        assistant = capture._clean(t.get("assistant", "") or "")
        if not (user or assistant):
            continue
        entry = {
            "seq": t.get("seq", i), "date": today, "ts": t.get("ts") or capture._iso_now(),
            "git": git, "user": user, "assistant": assistant,
            "files": t.get("files", []) or [], "uploaded": False,
        }
        source_ids = [s for s in t.get("source_message_ids", []) or [] if isinstance(s, str) and s]
        if source_ids:
            # Opt into the core's v2 identity only when the rollout gave us Codex's turn id: that is
            # what the live hook hashes too. A rollout without one keeps the legacy `session:seq`
            # id it has always had, rather than a v2 fallback that hashes nothing either side shares.
            entry["event_id_version"] = 2
            entry["source_message_ids"] = source_ids
        buf["turns"].append(entry)
    return buf


def _watch(url: str, token: str, ident: dict, batch_id: str, timeout: float) -> None:
    print("watching status (every 5s; Ctrl-C to stop) ...")
    while True:
        try:
            res = gbrain_client.call_tool(url, token, "becos_backfill_status",
                                          {"batch_id": batch_id}, timeout, extra_headers=ident)
        except gbrain_client.GbrainError as exc:
            print(f"  status error: {exc}")
            return
        roll = _tool_data(res).get("rollup", {})
        print("  queued={} running={} complete={} failed={}  turns {}/{}".format(
            roll.get("queued", 0), roll.get("running", 0), roll.get("complete", 0),
            roll.get("failed", 0), roll.get("turns_done", 0), roll.get("turns_total", 0)))
        if not roll.get("queued", 0) and not roll.get("running", 0):
            return
        time.sleep(5)


def _async_deliver(rows: list[dict], timeout: float, watch: bool, batch_id: str) -> int:
    """Hand each session to the connector's ``becos_backfill_upload`` and return — the server-side
    worker drains it turn-by-turn. Raises ``_ToolUnavailable`` when the connector has no such tool,
    so the caller can fall back to the synchronous drain."""
    url, token = capture._resolve_becos()
    ident = capture._becos_identity_headers()
    done = _uploaded_keys()
    queued = total_turns = already = 0
    deferred: list[str] = []
    print(f"\nuploading (async) as batch {batch_id} ...")
    for r in rows:
        if r["turns"] == 0 or r.get("skipped") or not r.get("scoped"):
            continue
        if (batch_id, r["session"]) in done:  # manifest says handed off — skip, no round-trip
            already += 1
            continue
        label = r["session"][:8]
        events = capture._build_events(_fresh_buffer(r["_session"]))
        if not events:  # nothing deliverable (e.g. cwd outside any repo) — NOT recorded, retryable
            print(f"  --    {label}  (nothing to deliver)")
            continue
        sessions = [{"session_id": r["session"],
                     "turns": [{"event_id": e["event_id"], "args": e["args"]} for e in events]}]
        try:
            gbrain_client.call_tool(url, token, "becos_backfill_upload",
                                    {"sessions": sessions, "batch_id": batch_id},
                                    timeout, extra_headers=ident)
        except gbrain_client.GbrainError as exc:
            if not exc.transport and any(h in str(exc).lower() for h in _UNAVAIL_HINTS):
                raise _ToolUnavailable() from exc
            deferred.append(r["session"])
            print(f"  DEFER {label}: {str(exc)[:70]}")
            continue
        _record_manifest(batch_id, r["session"], len(events))
        queued += 1
        total_turns += len(events)
        print(f"  queued {label}  ({len(events)} turns)")

    print(f"\nqueued {queued} sessions ({total_turns} turns) as batch {batch_id}; "
          f"already-done {already}; deferred {len(deferred)}.")
    print(f"poll:  becos_backfill_status(batch_id={batch_id!r})   (or re-run with --watch)")
    if watch:
        _watch(url, token, ident, batch_id, timeout)
    return 0 if not deferred else 1


def _sync_deliver(rows: list[dict], retries: int) -> int:
    """Fallback for connectors without the async tools: write each session's fresh buffer to a temp
    path and let the vendored ``capture._deliver`` drain it turn-by-turn (retry + backoff)."""
    tmp_dir = os.path.join(capture._cache_dir(), "cx-backfill-tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    print(f"\ndelivering (sync)...  (up to {retries} attempts per session)")
    delivered = 0
    deferred: list[str] = []
    for r in rows:
        if r["turns"] == 0 or r.get("skipped") or not r.get("scoped"):
            continue
        label = f"{r['project'][:30]:<32} {r['session'][:8]}"
        buf = _fresh_buffer(r["_session"])
        buf["done"] = True
        path = os.path.join(tmp_dir, f"{r['session']}.json")
        capture._save_buffer(path, buf)
        delay = 2.0
        landed = False
        for attempt in range(1, retries + 1):
            try:
                capture._deliver(path)  # locks/loads/delivers/saves; removes buffer on full success
            except Exception:  # noqa: BLE001 — never let one session abort the sweep; retry/backoff
                pass
            if not os.path.exists(path):
                landed = True
                break
            if attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        if landed:
            delivered += 1
            print(f"  ok   {label}  ({r['turns']} turns)")
        else:
            deferred.append(r["session"])
            print(f"  DEFER {label}  ({r['turns']} turns) — still timing out after {retries} tries")
    print(f"\ndelivered {delivered} sessions; deferred {len(deferred)}")
    if deferred:
        print("deferred (re-run to retry, it's idempotent):")
        for s in deferred:
            print(f"  {s}")
    return 0 if not deferred else 1


# ── dry-run table (Claude Code / Opencode parity) ────────────────────────────
def _print_breakdown(rows: list[dict]) -> "OrderedDict[str, dict]":
    """Print the per-repo → session → turns table over the DELIVERABLE (repo-scoped) sessions, and
    return that grouping. Non-repo and unrecognized sessions are summarised separately so the counts
    shown are exactly what ``--deliver`` will ingest."""
    scoped = [r for r in rows if r.get("scoped") and not r.get("skipped")]
    by: "OrderedDict[str, dict]" = OrderedDict()
    for r in scoped:
        p = by.setdefault(r["project"], {"sessions": 0, "turns": 0})
        p["sessions"] += 1
        p["turns"] += r["turns"]
    print(f"{'repository':<50}{'sessions':>9}{'turns':>8}")
    print("-" * 68)
    for name, v in by.items():
        print(f"{name[:48]:<50}{v['sessions']:>9}{v['turns']:>8}")
    print("-" * 68)
    print(f"{'TOTAL (deliverable)':<50}{sum(v['sessions'] for v in by.values()):>9}"
          f"{sum(v['turns'] for v in by.values()):>8}")
    print("\nrepositories → sessions → turns:")
    for name in by:
        print(f"  {name}  ({by[name]['sessions']} sessions, {by[name]['turns']} turns)")
        for r in sorted((r for r in scoped if r["project"] == name), key=lambda r: -r["turns"]):
            resumed = f"  ({r['files']} rollout files, merged)" if r.get("files", 1) > 1 else ""
            print(f"    {r['session']:<46}{r['turns']:>6} turns{resumed}")
    return by


def main() -> int:
    deliver = "--deliver" in sys.argv
    async_mode = "--async" in sys.argv or capture._flag("VONIC_BECOS_ASYNC")
    watch = "--watch" in sys.argv
    proj_filter = _arg_value("--project")
    sess_filter = _arg_values("--session")

    rows = discover()
    if proj_filter:
        rows = [r for r in rows if proj_filter.lower() in r["project"].lower()]
    if sess_filter:
        rows = [r for r in rows
                if r["session"] in sess_filter or any(r["session"].startswith(s) for s in sess_filter)]

    by = _print_breakdown(rows)

    unscoped = [r for r in rows if not r.get("scoped") and not r.get("skipped")]
    unrecognized = [r for r in rows if r.get("skipped")]
    if unscoped:
        u_turns = sum(r["turns"] for r in unscoped)
        print(f"\nnote: {len(unscoped)} session(s) / {u_turns} turn(s) ran outside any git repository "
              f"(e.g. Codex desktop scratch dirs) and will be skipped — repository memory only "
              f"ingests repo-scoped work.")
    if unrecognized:
        print(f"note: {len(unrecognized)} session file(s) were in an unrecognized format and will be "
              f"skipped (not parsed, not ingested).")

    if not deliver:
        if by:
            ex_proj = max(by, key=lambda p: by[p]["turns"])
            ex_rows = sorted((r for r in rows if r["project"] == ex_proj and not r.get("skipped")),
                             key=lambda r: -r["turns"])
            ex_sess = ex_rows[0]["session"][:8] if ex_rows else "<session-id>"
            print("\nto ingest, re-run with --deliver (--async recommended for large histories):")
            print("  whole history :  codecollab-cx backfill --deliver --async")
            print(f"  one project   :  codecollab-cx backfill --deliver --async --project {ex_proj}")
            print(f"  one session   :  codecollab-cx backfill --deliver --async --session {ex_sess}")
            print("  options       :  --watch (follow status)   ·   --campaign <name> (stable, resumable batch)")
            print(f"  via the slash :  /backfill --deliver --async --project {ex_proj}")
        print("\nDRY RUN — nothing delivered.")
        return 0

    # Backfill hands a whole session to the backend at once, so raise the default client timeout
    # well above live capture's.
    timeout = _arg_value("--timeout") or os.environ.get("VONIC_CODECOLLAB_TIMEOUT") or "240"
    os.environ["VONIC_CODECOLLAB_TIMEOUT"] = str(timeout)
    retries = max(1, int(_arg_value("--retries") or 4))

    if async_mode:
        try:
            return _async_deliver(rows, float(timeout), watch, _default_batch_id())
        except _ToolUnavailable:
            print("connector has no async backfill tools — falling back to the synchronous drain.")
    return _sync_deliver(rows, retries)


if __name__ == "__main__":
    sys.exit(main())
