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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — reuse becos config + identity resolution
import gbrain_client  # noqa: E402
import recall  # noqa: E402 — reuse the response-text extraction

_TIMEOUT = float(os.environ.get("VONIC_RECALL_TIMEOUT", "20"))

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
    return _record(result)


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
    """Render one event record as flat `key: value` lines, verbatim."""
    lines = [f"{k}: {evidence[k]}" for k in _FIELDS if evidence.get(k) not in (None, "")]
    extra = sorted(set(evidence) - set(_FIELDS) - {"evidence_type"})
    lines += [f"{k}: {evidence[k]}" for k in extra if evidence.get(k) not in (None, "")]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        sys.stderr.write(f"usage: {os.path.basename(__file__)} <event-id> [<event-id> ...]\n")
        return 2
    resolved = 0
    for event_id in argv:
        try:
            evidence = resolve(event_id)
        except gbrain_client.GbrainError as exc:
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
