"""One-off backfill of existing Claude Code sessions into memory.

codecollab captures sessions going forward, and fully backfills any session you *continue*
(the Stop hook reads the whole transcript). This sweeps sessions you will not reopen —
the transcripts under ``~/.claude/projects/<slug>/<session>.jsonl`` — through the same
parse -> record -> deliver path, so historical turns land in the backend too.

Safe by default: a DRY RUN just lists what it would ingest. Pass --deliver to actually ship
(needs the same env as live delivery: VONIC_BECOS_URL / VONIC_GBRAIN_URL, VONIC_TENANT_ID /
VONIC_USER_ID, tokens). Delivery is idempotent — re-running skips turns already buffered, and
gbrain overwrites pages by slug.

Robust for large sessions: unlike live capture (which ships one small turn at a time),
backfill sends a whole session body at once, so a big session can blow past the default 15s
client timeout or hit a transient upstream timeout. So --deliver uses a generous default
timeout and retries each session with backoff until it lands, and reports any that still
defer (re-running is safe — it retries only what didn't land).

    python3 backfill.py                      # dry run: per-project sessions + turn counts
    python3 backfill.py --project vonic-code # filter by project slug (substring)
    python3 backfill.py --project vonic-code --session d1f936d5 --session 56b90e21
                                             # narrow to specific sessions (id or prefix; repeatable
                                             # or comma-separated); adds a per-session turn breakdown
    python3 backfill.py --deliver            # actually ingest (retry + backoff per session)
    python3 backfill.py --deliver --timeout 300 --retries 6   # tune for very large histories

Async mode (``--async`` or ``VONIC_BECOS_ASYNC=1``, opt-in) hands each session to the connector's
``becos_backfill_upload`` and returns — a server-side worker drains it turn-by-turn — instead of
this process driving the whole drain synchronously. ``--watch`` then polls ``becos_backfill_status``
until the batch is done. If the connector doesn't advertise the tools, it falls back to the sync
drain, so the flag is safe on older deploys.

    python3 backfill.py --deliver --async            # upload + hand off, then exit
    python3 backfill.py --deliver --async --watch    # upload, then poll status to completion
    python3 backfill.py --deliver --async --campaign myrun   # name the campaign (batch id)

Bookkeeping: async runs use a STABLE batch id (``--campaign``, else ``backfill-<hostname>``), so
``becos_backfill_status(batch_id)`` reads as cumulative progress across runs. A local manifest
(``~/.cache/codecollab/backfill-manifest.jsonl``) records each handed-off session, so a re-run
skips what's already uploaded without a server round-trip; the server dedups anything that slips
through.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import socket
import time
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402  (sibling module — reuse its transcript parser + delivery)
import gbrain_client  # noqa: E402  (sibling — MCP client for the connector's backfill tools)

PROJECTS = os.path.expanduser("~/.claude/projects")


def _cwd_from_transcript(path: str) -> str:
    """The session's representative cwd — the DOMINANT one, not the first seen.

    Per-turn scope (in capture._record_turns) keys each turn by its own line's cwd; this value is
    only the buffer-level fallback + project slug. A session can span several directories, so the
    first line's cwd is a poor stand-in (it may be a one-off dir the real work never touched).
    The most frequent cwd is a far better default, and — being the directory most turns ran in —
    is usually a real repo, which keeps the fallback scoped rather than dropping the session.
    """
    from collections import Counter
    counts: Counter[str] = Counter()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                obj = json.loads(line)
                if isinstance(obj, dict) and obj.get("cwd"):
                    counts[obj["cwd"]] += 1
    except Exception:
        pass
    return counts.most_common(1)[0][0] if counts else os.getcwd()


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


def _deliver_with_retry(buffer_path: str, retries: int) -> bool:
    """Deliver one session buffer, retrying while it defers.

    ``capture._deliver`` removes the buffer on full success and keeps it (with its un-sent
    turns) on a timeout or upstream error, so the buffer's continued existence is our
    'still pending' signal. Back off between attempts to let a loaded backend catch up."""
    delay = 2.0
    for attempt in range(1, retries + 1):
        try:
            capture._deliver(buffer_path)  # swallows transport errors; keeps buffer on failure
        except Exception:  # noqa: BLE001 — never let one session abort the sweep; retry/backoff
            pass
        if not os.path.exists(buffer_path):
            return True
        if attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    return not os.path.exists(buffer_path)


def discover() -> list[dict]:
    rows = []
    for tp in sorted(glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))):
        try:
            turns = [t for t in capture._parse_session(tp) if t.get("user") or t.get("assistant")]
        except Exception:
            turns = []
        rows.append(
            {
                "project": os.path.basename(os.path.dirname(tp)),
                "session": os.path.splitext(os.path.basename(tp))[0],
                "path": tp,
                "turns": len(turns),
            }
        )
    return rows


# Canonical in capture.py (live delivery uses the same enqueue tool + fallback); reused here so the
# "unknown tool" → sync-fallback contract stays identical across the backfill and live paths.
_ToolUnavailable = capture._ToolUnavailable
_UNAVAIL_HINTS = capture._UNAVAIL_HINTS


def _remove_buffer(path: str) -> None:
    """Drop a handed-off session buffer and its lock (best effort)."""
    for p in (path, path + ".lock"):
        try:
            os.remove(p)
        except OSError:
            pass


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
    """A STABLE batch id so re-runs land in the same campaign: the server skips sessions already
    enqueued (enqueue is idempotent on tenant+batch+session) and `becos_backfill_status(batch_id)`
    reads as cumulative progress. Override with --campaign to run a distinct campaign."""
    return _arg_value("--campaign") or f"backfill-{socket.gethostname().split('.')[0]}"


def _manifest_path() -> str:
    """Local durable ledger of handed-off sessions (one JSON line each), under the codecollab
    cache. Lets a re-run skip what's already uploaded without a server round-trip."""
    return os.path.join(capture._cache_dir(), "backfill-manifest.jsonl")


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
        with open(_manifest_path(), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


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
    """Hand each session to the connector's `becos_backfill_upload` and return — the server-side
    worker drains it turn-by-turn. `batch_id` is STABLE per campaign, so `becos_backfill_status`
    reads as cumulative progress and re-runs skip what's already done: the local manifest skips
    handed-off sessions without a round-trip, and the server dedups the rest (enqueue is idempotent
    on tenant+batch+session; each turn dedups by event_id). Raises `_ToolUnavailable` when the
    connector has no such tool, so the caller can fall back to the synchronous drain."""
    url, token = capture._resolve_becos()
    ident = capture._becos_identity_headers()
    done = _uploaded_keys()
    uploaded, total_turns, skipped, already = 0, 0, [], 0
    print(f"\nuploading (async) as batch {batch_id} ...")
    for r in rows:
        if r["turns"] == 0:
            continue
        if (batch_id, r["session"]) in done:   # manifest says handed off — skip, no round-trip
            already += 1
            continue
        label = r["session"][:8]
        event = {"session_id": r["session"], "transcript_path": r["path"],
                 "cwd": _cwd_from_transcript(r["path"])}
        try:
            buffer_path = capture._record_turns(event, done=True)
        except Exception as exc:  # noqa: BLE001 — one bad transcript must not stop the sweep
            skipped.append(r["session"])
            print(f"  err   {label}: {str(exc)[:70]}")
            continue
        if not buffer_path:
            print(f"  ok    {label}  (already current)")
            continue
        with capture._locked(buffer_path):
            buf = capture._load_buffer(buffer_path)
            events = capture._build_events(buf)
            capture._save_buffer(buffer_path, buf)   # persist any drop marks
        if not events:
            _remove_buffer(buffer_path)
            _record_manifest(batch_id, r["session"], 0)   # nothing to deliver — but don't recheck
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
            skipped.append(r["session"])   # keep the buffer for a later retry
            print(f"  DEFER {label}: {str(exc)[:70]}")
            continue
        _remove_buffer(buffer_path)          # handed off — the connector owns delivery now
        _record_manifest(batch_id, r["session"], len(events))
        uploaded += 1
        total_turns += len(events)
        print(f"  queued {label}  ({len(events)} turns)")

    print(f"\nqueued {uploaded} sessions ({total_turns} turns) as batch {batch_id}; "
          f"already-done {already}; skipped {len(skipped)}.")
    print(f"poll:  becos_backfill_status(batch_id={batch_id!r})   (or re-run with --watch)")
    if watch:
        _watch(url, token, ident, batch_id, timeout)
    return 0 if not skipped else 1


def _sync_deliver(rows: list[dict], retries: int) -> int:
    print(f"\ndelivering...  (up to {retries} attempts per session)")
    delivered, deferred, errored = 0, [], []
    for r in rows:
        if r["turns"] == 0:
            continue
        label = f"{r['project'][:30]:<32} {r['session'][:8]}"
        event = {
            "session_id": r["session"],
            "transcript_path": r["path"],
            "cwd": _cwd_from_transcript(r["path"]),
        }
        try:
            buffer_path = capture._record_turns(event, done=True)
        except Exception as exc:  # noqa: BLE001 — one bad transcript must not stop the sweep
            errored.append(r["session"])
            print(f"  err  {label}: {str(exc)[:80]}")
            continue
        if not buffer_path:  # nothing pending (already current) — count as landed
            delivered += 1
            print(f"  ok   {label}  ({r['turns']} turns, already current)")
            continue
        if _deliver_with_retry(buffer_path, retries):
            delivered += 1
            print(f"  ok   {label}  ({r['turns']} turns)")
        else:
            deferred.append(r["session"])
            print(f"  DEFER {label}  ({r['turns']} turns) — still timing out after {retries} tries")

    print(f"\ndelivered {delivered} sessions; deferred {len(deferred)}; errored {len(errored)}")
    if deferred:
        print("deferred (upstream kept timing out — re-run to retry, it's idempotent):")
        for s in deferred:
            print(f"  {s}")
    return 0 if not (deferred or errored) else 1


def main() -> int:
    deliver = "--deliver" in sys.argv
    async_mode = "--async" in sys.argv or capture._flag("VONIC_BECOS_ASYNC")
    watch = "--watch" in sys.argv
    proj_filter = _arg_value("--project")
    sess_filter = _arg_values("--session")   # exact ids or prefixes; composes with --project

    rows = discover()
    if proj_filter:
        rows = [r for r in rows if proj_filter in r["project"]]
    if sess_filter:
        rows = [r for r in rows
                if r["session"] in sess_filter or any(r["session"].startswith(s) for s in sess_filter)]

    # ── breakdown: projects, then (when a scope is given) sessions → turns ───────
    by = OrderedDict()
    for r in rows:
        p = by.setdefault(r["project"], {"sessions": 0, "turns": 0})
        p["sessions"] += 1
        p["turns"] += r["turns"]

    print(f"{'project':<50}{'sessions':>9}{'turns':>8}")
    print("-" * 68)
    for name, v in by.items():
        print(f"{name[:48]:<50}{v['sessions']:>9}{v['turns']:>8}")
    print("-" * 68)
    print(f"{'TOTAL':<50}{sum(v['sessions'] for v in by.values()):>9}{sum(v['turns'] for v in by.values()):>8}")

    # Full breakdown: every project → its sessions → turns, so the user can decide what to send
    # without having to narrow first (a bare run shows everything; a scoped run is just shorter).
    print("\nprojects → sessions → turns:")
    for name in by:
        print(f"  {name}  ({by[name]['sessions']} sessions, {by[name]['turns']} turns)")
        for r in sorted((r for r in rows if r["project"] == name), key=lambda r: -r["turns"]):
            print(f"    {r['session']:<46}{r['turns']:>6} turns")

    if not deliver:
        # Ready-to-run commands, using real values from the largest in-scope project/session.
        if by:
            ex_slug = max(by, key=lambda p: by[p]["turns"])
            ex_proj = "-".join(ex_slug.strip("-").split("-")[-2:])
            ex_rows = sorted((r for r in rows if r["project"] == ex_slug), key=lambda r: -r["turns"])
            ex_sess = ex_rows[0]["session"][:8] if ex_rows else "<session-id>"
            print("\nto ingest, re-run with --deliver (--async recommended for large histories):")
            print("  whole history :  python3 backfill.py --deliver --async")
            print(f"  one project   :  python3 backfill.py --deliver --async --project {ex_proj}")
            print(f"  one session   :  python3 backfill.py --deliver --async --project {ex_proj} --session {ex_sess}")
            print("  options       :  --watch (follow status)   ·   --campaign <name> (stable, resumable batch)")
            print(f"  via the skill :  /codecollab:backfill {ex_proj} {ex_sess} --async")
        print("\nDRY RUN — nothing delivered.")
        return 0

    # Backfill hands a whole session to the backend at once, so raise the default client timeout
    # well above live capture's 15s.
    timeout = _arg_value("--timeout") or os.environ.get("VONIC_CODECOLLAB_TIMEOUT") or "240"
    os.environ["VONIC_CODECOLLAB_TIMEOUT"] = str(timeout)
    retries = max(1, int(_arg_value("--retries") or 4))

    if async_mode:
        # Upload + hand off to the server-side worker. Fall back to the sync drain only when the
        # connector genuinely lacks the tools (older deploy), so this stays additive.
        try:
            return _async_deliver(rows, float(timeout), watch, _default_batch_id())
        except _ToolUnavailable:
            print("connector has no async backfill tools — falling back to the synchronous drain.")
    return _sync_deliver(rows, retries)


if __name__ == "__main__":
    sys.exit(main())
