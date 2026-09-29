#!/usr/bin/env python3
"""Resolve one cited recall source (event id) back to its stored evidence.

Recalled memory injected by recall.py cites exact ledger event IDs, because the server's
QUERY_INSTRUCTIONS require every material claim to carry one. Without a way to resolve those
IDs they are unverifiable, and an unverifiable citation is indistinguishable from a fabricated
one -- which makes accurate recall look like a prompt-injection attempt and get discarded.

This is that resolver. It calls `vonic_resolve_event`, which host-reads the record and returns it
verbatim -- no model call, no summarisation, no tool budget. Determinism is the point: a
paraphrased answer would reintroduce the doubt the check exists to remove, and a lookup that cost
an agent run would be too expensive to use on more than one citation.

Scope is the same one the query path enforces (`ScopedLedgerTools.get()`: tenant filter plus a
re-check against the full request context), so an unknown or out-of-scope id resolves to nothing
rather than leaking.

Usage:
    python3 show_source.py <event-id> [<event-id> ...]

Exit codes: 0 resolved (or partially resolved), 1 nothing resolved, 2 misconfigured.
Env: reuses the becos target/identity that capture.py and recall.py already need
(VONIC_BECOS_URL + token). VONIC_RECALL_TIMEOUT bounds each lookup (default 20s).
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — reuse becos config + identity resolution
import gbrain_client  # noqa: E402
import recall  # noqa: E402 — reuse the response-text extraction

_TIMEOUT = float(os.environ.get("VONIC_RECALL_TIMEOUT", "20"))
_LOGCHARS = int(os.environ.get("VONIC_RESOLVE_LOGCHARS", "500"))

# Field order for rendering an event record; anything else the server returns is printed after.
_FIELDS = (
    "event_id", "event_type", "lifecycle_status", "lifecycle_label",
    "repo_name", "branch_name", "author_name", "occurred_at", "git_commit_id",
    "observed_content", "inferred_feature", "feature_confidence",
)


def resolve(event_id: str) -> dict | None:
    """Return `event_id`'s stored evidence, or None when it is not in scope.

    Deterministic: `vonic_resolve_event` host-reads the record and returns it verbatim, with no
    model call and no summarisation. That matters here -- a paraphrased answer would reintroduce
    the doubt this lookup exists to remove -- and it means a check costs a DB read, not an agent
    run, so verifying several citations is cheap.
    """
    url, token = capture._resolve_becos()
    result = gbrain_client.call_tool(
        url,
        token,
        "vonic_resolve_event",
        {"event_id": event_id},
        _TIMEOUT,
        extra_headers=capture._becos_identity_headers(),
    )
    evidence = _record(result)
    _log(event_id, "resolved" if evidence else "not-found", evidence)
    return evidence


def _one_line(evidence: dict) -> str:
    """A short, single-line gist of a resolved record for the compact log/snippet.

    Prefers the canonical text (a decision's statement or a fact's text); the whole bundle stays
    available in the backend audit log when VONIC_BACKEND_AUDIT_LOG=1, so this is only a pointer."""
    record = evidence.get("record") if isinstance(evidence.get("record"), dict) else evidence
    kind = evidence.get("kind") or record.get("kind") or "record"
    text = (record.get("decision") or record.get("fact_text") or "").replace("\n", " ").strip()
    return f"{kind}: {text}" if text else str(kind)


def _log(event_id: str, outcome: str, evidence: dict | None = None) -> None:
    """Append a one-line resolve record to ~/.cache/codecollab/resolve.log.

    The sibling of recall.py's recall.log: debug-gated (VONIC_CODECOLLAB_DEBUG=1), best-effort, and
    never raises. It is ground truth that a resolve fired and how it landed, independent of what the
    model reports; the full evidence bundle lives in backend-api.log when the audit log is enabled.
    """
    if os.environ.get("VONIC_CODECOLLAB_DEBUG") != "1":
        return
    try:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        directory = os.path.join(base, "codecollab")
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        tag = os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc")
        line = f"{stamp}  {tag}/v{capture._plugin_version()}  event_id={event_id} outcome={outcome}"
        if evidence is not None:
            line += f"  ::  {_one_line(evidence)[:_LOGCHARS]}"
        with open(os.path.join(directory, "resolve.log"), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:  # noqa: BLE001 — logging must never break a resolve
        pass


def _record(result: dict) -> dict | None:
    """Decode a structured tool result into the event record, or None.

    Deliberately not recall._answer_text: that unwraps a natural-language `{"result": "..."}`
    answer, whereas this tool returns the record itself (or null). Anything that is not a
    non-empty object -- null, a bare value, malformed JSON -- means "not in scope".
    """
    parts = [
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    payload = "\n".join(part for part in parts if part).strip()
    if not payload:
        return None
    try:
        evidence = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if isinstance(evidence, dict) and "result" in evidence:  # tolerate a wrapped result
        evidence = evidence["result"]
    return evidence if isinstance(evidence, dict) and evidence else None


def render(evidence: dict) -> str:
    """Render one resolved citation for the terminal.

    Two shapes are accepted. A becos ``vonic.resolve.v2`` evidence bundle (canonical record plus
    provenance/source/tool_activity/relations sections) is rendered as grouped, readable blocks; any
    other shape is a legacy flat event record, rendered verbatim as ``key: value`` lines. Detection
    is by ``schema_version`` so an older server (or a different resolver) keeps working unchanged.
    """
    if evidence.get("schema_version") == "vonic.resolve.v2":
        return _render_bundle(evidence)
    lines = [f"{k}: {evidence[k]}" for k in _FIELDS if evidence.get(k) not in (None, "")]
    extra = sorted(set(evidence) - set(_FIELDS) - {"evidence_type"})
    lines += [f"{k}: {evidence[k]}" for k in extra if evidence.get(k) not in (None, "")]
    return "\n".join(lines)


# Canonical-record fields worth surfacing, per kind. Everything else stays in the raw record but is
# not printed — the bundle is a briefing, not a dump.
_RECORD_FIELDS = {
    "fact": ("fact_text", "kind", "decision_status", "evidence_status", "repo_id",
             "branch", "revision", "confidence"),
    "decision": ("decision", "decision_status", "evidence_status", "repo_id",
                 "branch", "revision"),
}
_PROVENANCE_FIELDS = (
    "event_id", "event_type", "repo_name", "branch_name", "git_commit_id",
    "occurred_at", "author_name",
)


def _render_bundle(bundle: dict) -> str:
    blocks: list[str] = []
    citation = bundle.get("citation") or {}
    blocks.append(f"kind: {bundle.get('kind')}\nid: {citation.get('id')}")

    record = bundle.get("record") or {}
    fields = _RECORD_FIELDS.get(bundle.get("kind"), ())
    rec_lines = [f"  {k}: {record[k]}" for k in fields if record.get(k) not in (None, "")]
    if rec_lines:
        blocks.append("record:\n" + "\n".join(rec_lines))

    status = bundle.get("status") or {}
    if status:
        cur = "current" if status.get("is_current") else "superseded/corrected"
        blocks.append(f"status: {cur} ({status.get('decision_status')}, "
                      f"{status.get('evidence_status')})")

    prov = bundle.get("provenance") or {}
    prov_lines = [f"  {k}: {prov[k]}" for k in _PROVENANCE_FIELDS if prov.get(k) not in (None, "")]
    if prov_lines:
        blocks.append("provenance:\n" + "\n".join(prov_lines))

    linked = bundle.get("linked") or {}
    if linked.get("record"):
        lr = linked["record"]
        text = lr.get("decision") or lr.get("fact_text") or ""
        lid = lr.get("decision_id") or lr.get("fact_id") or ""
        blocks.append(f"linked {linked.get('kind')}: {lid}\n  {text}")

    source = bundle.get("source") or {}
    if source:
        src_lines: list[str] = []
        for cf in source.get("changed_files", []):
            src_lines.append(f"  {cf.get('status', '?')} {cf.get('path')} "
                             f"(+{cf.get('additions', 0)}/-{cf.get('deletions', 0)})")
        for key in ("files_modified", "files_read", "mentioned_symbols", "entities"):
            vals = source.get(key)
            if vals:
                src_lines.append(f"  {key}: {', '.join(str(v) for v in vals)}")
        if source.get("observed_content"):
            src_lines.append(f"  observed_content: {source['observed_content']}")
        if src_lines:
            blocks.append("source:\n" + "\n".join(src_lines))

    activity = bundle.get("tool_activity") or []
    if activity:
        act_lines = []
        for ev in activity:
            head = f"  {ev.get('tool')}[{ev.get('category')}/{ev.get('status')}]"
            target = f" {ev['target']}" if ev.get("target") else ""
            summary = f": {ev['summary']}" if ev.get("summary") else ""
            err = f" !! {ev['error']}" if ev.get("error") else ""
            act_lines.append(f"{head}{target}{summary}{err}")
        blocks.append("tool_activity:\n" + "\n".join(act_lines))

    relations = bundle.get("relations") or {}
    rel_lines = [
        f"  {direction}: {', '.join(edge.get('id', '') for edge in edges)}"
        for direction, edges in relations.items() if edges
    ]
    if rel_lines:
        blocks.append("relations:\n" + "\n".join(rel_lines))

    truncated = (bundle.get("limits") or {}).get("truncated") or {}
    if truncated:
        blocks.append(f"(truncated: {', '.join(sorted(truncated))})")

    return "\n".join(blocks)


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(f"usage: {os.path.basename(__file__)} <event-id> [<event-id> ...]\n")
        return 2
    resolved = 0
    for event_id in argv:
        try:
            evidence = resolve(event_id)
        except gbrain_client.GbrainError as exc:
            _log(event_id, "failed")
            sys.stderr.write(f"{event_id}: lookup failed: {exc}\n")
            continue
        if evidence is None:
            print(f"{event_id}: not found in scope (unknown id, or outside your access)\n")
            continue
        resolved += 1
        print(f"=== {event_id} ===\n{render(evidence)}\n")
    return 0 if resolved else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except gbrain_client.GbrainError as exc:  # misconfiguration (no url/token)
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)
