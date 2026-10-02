#!/usr/bin/env python3
"""UserPromptSubmit hook: recall from the tenant's becos brain on every prompt.

Fires *before* Claude sees the prompt: calls the becos `vonic_query` tool with the
prompt text and injects the answer as UserPromptSubmit `additionalContext`, so Claude
always has the relevant brain context without deciding to fetch it.

Direction is the opposite of capture.py: this pulls memory IN; capture ships turns OUT.
Reuses capture.py's becos target + identity resolution (Mode A: identity from the
validated gateway token; Mode B: X-Tenant-ID / X-User-ID from env) and gbrain_client's
MCP transport — nothing new to configure beyond what delivery already needs.

FAIL-OPEN: any error/timeout/misconfig -> exit 0 with no output. A recall hook must
never block or delay the user's prompt beyond its own timeout, and never break it.

The digest is injected inside a `<recalled-memory>` element naming its source and scope, and
the server-side citations it carries are resolvable with show_source.py. Both exist so recalled
memory can be WEIGHED as evidence: unattributed, unverifiable assertions arriving under Claude
Code's "hook additional context" label read as a prompt-injection attempt and get discarded.
The framing is descriptive only — it must never instruct the reader to trust the contents.

The query carries `metadata.event` scoping it to the current checkout (repo + branch), which
routes it to repository memory — the same events capture.py writes. Without a resolvable repo it
falls back to a general-memory query, and only then is the prompt wrapped in a recall instruction
so the agentic path returns a digest of relevant memory rather than *answering* the prompt
(un-framed, agentic answers to meta/chatty prompts get injected as noise). The repository path
does its own framing server-side, so the instruction would only pollute its search text.

Env:
  VONIC_RECALL_ENABLED   "1" (default) to run; anything else disables the hook.
  VONIC_RECALL_SCOPE     "branch" (default) scope recall to repo + branch · "repo" repo only,
                         so a feature branch still recalls the repo's history · "off" disable
                         recall (an unscoped query would hit the general brain, so it is skipped).
  VONIC_RECALL_TIMEOUT   seconds for the vonic_query call (default 20).
  VONIC_RECALL_MAXCHARS  cap on injected chars (default 12000, hard maximum 20000).
  VONIC_RECALL_LOGCHARS  chars of the recalled response logged per run (default 500).
  VONIC_CODECOLLAB_DEBUG "1" to log failures + injected-response snippets to
                         ~/.cache/codecollab/recall.log (and failures to stderr).
  VONIC_RECALL_NOTIFY    "1" (default) to fire a rate-limited OS notification when the
                         gateway session has expired (a 401 that survives the re-mint
                         retry); "0" disables the notification. The in-context banner
                         still fires regardless.
  VONIC_RECALL_NOTIFY_INTERVAL  seconds between auth-expiry notifications (default 3600),
                         so a broken session warns once/hour, not once/prompt.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — reuse becos config + identity resolution
import gbrain_client  # noqa: E402
import provenance  # noqa: E402 — repo-provenance citation grammar

_DEFAULT_MAXCHARS = 12000
_HARD_MAXCHARS = 20000
_DEFAULT_TIMEOUT = 20.0
_DEFAULT_LOGCHARS = 500
_DEFAULT_NOTIFY_INTERVAL = 3600.0


def _parse_positive_float(value: str | None, default: float) -> float:
    """Parse a finite positive float, falling back for unsafe optional configuration."""
    try:
        parsed = float((value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed > 0 else default


def _parse_positive_int(value: str | None, default: int) -> int:
    """Parse a positive integer, falling back for unsafe optional configuration."""
    try:
        parsed = int((value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _parse_recall_maxchars(value: str | None) -> int:
    """Parse the defensive recall-text cap without letting bad configuration break recall."""
    try:
        parsed = int((value or "").strip())
    except (TypeError, ValueError):
        return _DEFAULT_MAXCHARS
    if parsed <= 0:
        return _DEFAULT_MAXCHARS
    return min(parsed, _HARD_MAXCHARS)


def _truncate_answer(answer: str, cap: int) -> str:
    """Cap opaque recall text, keeping a truncation marker inside the selected budget."""
    if len(answer) <= cap:
        return answer
    marker = f"\n…[truncated to {cap} chars]"
    if len(marker) > cap:
        marker = "…"[:cap]
    return answer[:cap - len(marker)] + marker


_TIMEOUT = _parse_positive_float(os.environ.get("VONIC_RECALL_TIMEOUT"), _DEFAULT_TIMEOUT)
_MAXCHARS = _parse_recall_maxchars(os.environ.get("VONIC_RECALL_MAXCHARS"))
_LOGCHARS = _parse_positive_int(os.environ.get("VONIC_RECALL_LOGCHARS"), _DEFAULT_LOGCHARS)
_NOTIFY_INTERVAL = _parse_positive_float(
    os.environ.get("VONIC_RECALL_NOTIFY_INTERVAL"), _DEFAULT_NOTIFY_INTERVAL
)

# Framing sent to vonic_query so it RECALLS relevant memory instead of answering the prompt
# itself (vonic_query is an agentic assistant — un-framed, it answers, which for meta/chatty
# prompts injects noise). This asks for a memory digest, or nothing when the brain is empty.
_RECALL_INSTRUCTION = (
    "You are a memory-recall step, not a chat assistant. Recall only relevant stored context "
    "(past decisions, facts, prior sessions, people/projects) that would help with the user "
    "message below. Return a concise digest of the relevant memory only. If nothing relevant is "
    "stored, return an empty response. Do NOT answer or address the message yourself.\n\n"
    "User message: "
)
# Short responses starting like these are "nothing relevant" non-answers -> suppress injection.
_EMPTY_PREFIXES = (
    "no relevant", "nothing relevant", "no stored", "nothing stored", "no memory",
    "none", "empty", "(empty", "i don't have", "i do not have", "there is no",
)

# Repository-memory scope for the query: `branch` (default) repo + branch · `repo` repo only, so a
# feature branch still recalls the whole repo's history · `off` no recall at all (see main()).
_SCOPE = os.environ.get("VONIC_RECALL_SCOPE", "branch").strip().lower()

# Deploy-then-flip gate for forwarding `caller_agent` on the query metadata. DEFAULTS OFF: a
# vonic-agent that predates migration 0019_query_caller_agent forbids the extra key and would fail
# every recall. Set to "1" only after the server carrying that field is deployed. See _query_scope.
_FORWARD_CALLER_AGENT = os.environ.get("VONIC_CODECOLLAB_FORWARD_CALLER_AGENT", "0") == "1"

# The server returns its routing/query errors as ORDINARY STRINGS on the same channel as an
# answer. Injecting one would hand the model "Repository memory is unavailable." *as recalled
# memory*, so they are suppressed exactly like an empty result. Kept in sync with vonic_agent's
# api/memory_routing.py + repository_memory/query_service.py.
#
# Matched with `in`, not `startswith`: the server PREPENDS VONIC_QUERY_CONTEXT_NOTICE to its
# answers, so a sentinel never lands at position 0 and a startswith check silently passed the
# error straight through into the model's context.
_SERVER_SENTINELS = (
    "error: repository memory is unavailable.",
    "repository query: exceeded its search budget before answering.",
    "repository query: no matching evidence found.",
    "error: repository query response omitted evidence citations.",
)

# Opening of the server's provenance header (vonic_agent api/memory_routing.py
# REPOSITORY_RECALL_HEADER). Only the stable first clause is matched, so re-wording its tail
# cannot break detection, and an older server that sends no header degrades to a no-op.
_CONTEXT_NOTICE_OPENING = "recalled from captured claude code sessions"

# A host-injected startup handoff at the head of the prompt. OpenCode's Claude bridge has no other
# channel, so the OpenCode plugin prefixes `<repository-handoff>…</repository-handoff>` onto one user
# message (becos-oc-plugin src/provenance.ts formatHandoff). It is ~11k chars of prior-session
# evidence, not the question: searched as-is it dominates the recall query. Only a LEADING block is
# matched; the model still receives the prompt unchanged, this only shapes the search text.
# The block ends at a closing tag on a line of its own, as formatHandoff writes it: the evidence
# inside is one-line JSON that can quote the tags (prior turns discussing the handoff), and stopping
# at the first quoted `</repository-handoff>` would leave the rest of the block in the search text.
_LEADING_HANDOFF_RE = re.compile(
    r"\A\s*<repository-handoff>\n.*?\n</repository-handoff>[ \t]*(?:\n|\Z)\s*", re.DOTALL
)


def _strip_handoff(prompt: str) -> str:
    """`prompt` without a leading `<repository-handoff>` block: the text recall searches on."""
    return _LEADING_HANDOFF_RE.sub("", prompt, count=1)


class AuthExpired(Exception):
    """A 401 that survived the re-mint retry: the gateway *session* is expired (not just a
    short-lived JWT), so recall will keep failing until the user re-authenticates (OTP).

    Distinct from transient failures (timeout, server down) which self-heal on the next
    prompt — this one needs a human, so the hook surfaces it LOUDLY (visible banner + a
    rate-limited OS notification) instead of failing silently and looking like an empty brain.
    """


# Injected in place of memory when auth has expired, so the model can TELL the user recall is
# down — distinct from the legitimate 'nothing relevant' empty case, which injects nothing.
_AUTH_BANNER = (
    '<brain-recall-status kind="auth-expired">\n'
    "NOTE TO ASSISTANT: memory recall FAILED for this prompt — the becos gateway session has "
    "expired (HTTP 401), so no brain memory could be retrieved. This is a retrieval failure, "
    "NOT an empty brain. Briefly tell the user their memory recall is offline and that "
    "re-authenticating (the codecollab login / connect step, via OTP) restores it.\n"
    "</brain-recall-status>"
)


def _debug(msg: str) -> None:
    if os.environ.get("VONIC_CODECOLLAB_DEBUG") == "1":
        sys.stderr.write(f"[codecollab recall] {msg}\n")


def _log(outcome: str) -> None:
    """Append a one-line verification record to ~/.cache/codecollab/recall.log.

    Debug-gated (VONIC_CODECOLLAB_DEBUG=1). Lets you `tail -f` the log to confirm the
    hook fires per prompt — ground truth, independent of what the model reports.
    Best-effort; never raises.
    """
    if os.environ.get("VONIC_CODECOLLAB_DEBUG") != "1":
        return
    try:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        directory = os.path.join(base, "codecollab")
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        tag = os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc")
        source = os.environ.get("VONIC_CODECOLLAB_RECALL_SOURCE", "unknown")
        mode = os.environ.get("VONIC_CODECOLLAB_CALLER_MODE", "unknown")
        with open(os.path.join(directory, "recall.log"), "a", encoding="utf-8") as handle:
            # self-report the client + running build, so the log proves which cached copy fired.
            handle.write(
                f"{stamp}  {tag}/v{capture._plugin_version()}  "
                f"source={source} mode={mode}  {outcome}\n"
            )
    except Exception:  # noqa: BLE001 — logging must never break the hook
        pass


def _notify_auth_expired() -> None:
    """Fire a rate-limited OS notification that recall auth has expired.

    The reliable out-of-band channel: unlike the injected banner, it doesn't depend on the
    model choosing to relay it. Rate-limited to once per `_NOTIFY_INTERVAL` (default 1h) via
    an on-disk stamp, since each hook run is a fresh process with no in-memory session state.
    Best-effort and non-blocking; never raises.
    """
    if os.environ.get("VONIC_RECALL_NOTIFY", "1") != "1":
        return
    try:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        directory = os.path.join(base, "codecollab")
        os.makedirs(directory, exist_ok=True)
        stamp = os.path.join(directory, ".auth-notify-stamp")
        now = time.time()
        try:
            if now - os.path.getmtime(stamp) < _NOTIFY_INTERVAL:
                return  # notified recently — stay quiet
        except OSError:
            pass  # no stamp yet -> notify
        # Touch the stamp BEFORE notifying so a slow/failed notify still rate-limits.
        with open(stamp, "w", encoding="utf-8") as handle:
            handle.write(str(int(now)))
        if sys.platform != "darwin":
            return  # only macOS notifications supported for now
        import subprocess

        title = "codecollab: brain recall offline"
        text = "Gateway session expired (401). Re-authenticate to restore memory recall."
        subprocess.Popen(  # noqa: S603 — fixed args, no shell, constants only
            ["osascript", "-e",
             f"display notification {json.dumps(text)} with title {json.dumps(title)}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 — a notification must never break the hook
        pass


def _emit_context(context: str) -> None:
    """Emit UserPromptSubmit additionalContext — the one channel that reaches the model.

    Recalled memory is wrapped in an explicit `<recalled-memory>` element naming its source and
    scope. This REVERSES the earlier design, which passed the digest through unwrapped so it would
    "read as part of the user's turn". That backfired: Claude Code stores this as its own
    `hook_additional_context` attachment and adds its own visible label at render time (neither is
    suppressible from a plugin), so an unattributed digest arrived under a third-party label with
    no provenance — which reads exactly like an injected payload and got discarded wholesale,
    taking accurate recall with it. Honest, self-consistent provenance survives scrutiny; content
    trying to pass as the user's own words does not.

    The wrapper is descriptive, never imperative. It must not tell the reader to trust the
    contents: "this context is trusted" / "do not ignore" is the shape of an injection payload and
    makes dismissal MORE likely, not less. Trust comes from the citations being resolvable
    (show_source.py), not from assertion.

    A hook cannot append to the user's typed message: `additionalContext` is the only context
    field in the UserPromptSubmit output schema, and the sole rewrite field (`updatedInput`) is
    PreToolUse-only.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }))


def _wrap(answer: str, scope: dict | None) -> str:
    """Wrap a recalled digest in a provenance element stating source, scope, and how to verify."""
    event = (scope or {}).get("event") or {}
    attrs = ' source="codecollab"'
    for key, attr in (("repo_name", "repo"), ("branch_name", "branch")):
        value = event.get(key)
        if value:
            attrs += f' {attr}="{_xml_attr(str(value))}"'
    if not event:
        attrs += ' scope="general-memory"'
    return (
        f"<recalled-memory{attrs}>\n"
        f"{answer}\n"
        "</recalled-memory>"
    )


def _xml_attr(value: str) -> str:
    """Escape a value for use in a double-quoted attribute, so a repo/branch name can't break out."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def _answer_text(result: dict) -> str:
    """Join the text parts of an MCP tool result into the server's answer.

    Shared with show_source.py, which resolves a cited event id through the same tool.
    """
    parts: list[str] = []
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text", "")
            try:  # the tool wraps its answer as {"result": "..."}
                decoded = json.loads(text)
            except (ValueError, TypeError):
                pass
            else:
                # Only an object carries the wrapper. A tool returning null or a bare value
                # decodes to None/list/str, and calling .get() on that used to raise.
                if isinstance(decoded, dict):
                    text = decoded.get("result", text)
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _strip_context_notice(t: str) -> str:
    """Drop the server's leading provenance header from a lowercased answer.

    Detection only — the injected text is left untouched. Without this the header makes the
    `_EMPTY_PREFIXES` test below unreachable: the answer no longer *starts* with "no relevant …"
    and is never under the 240-char bar. Degrades to a no-op if the header is absent or re-worded,
    so it stays correct against servers older or newer than this plugin.
    """
    if not t.startswith(_CONTEXT_NOTICE_OPENING):
        return t
    _, sep, rest = t.partition("\n\n")   # the header is one paragraph; the answer follows it
    return rest.strip() if sep else ""


def _looks_empty(text: str) -> bool:
    """True if the recall response is a non-answer to suppress: a server error sentinel, or a
    short 'nothing relevant' reply."""
    t = _strip_context_notice(text.strip().lower())
    if len(t) < 12:
        return True
    if any(sentinel in t for sentinel in _SERVER_SENTINELS):
        return True
    return len(t) < 240 and t.startswith(_EMPTY_PREFIXES)


def _query_scope(
    cwd: str, session_id: str | None = None, caller_agent: str | None = None
) -> dict | None:
    """`metadata.event` scoping recall to this checkout, or None for a general-memory query.

    Only the fields the query model accepts — no identity, content, timestamps, changed files, or
    event type; it is strict and rejects anything else. At least one field is required, so a cwd
    that resolves to no repo returns None — and the caller then skips recall entirely, since an
    unscoped query would route to the general brain (which codecollab must never read).
    """
    if _SCOPE == "off" or not capture._is_git(cwd):
        return None
    event = {}
    repo = capture._repo_slug(cwd)
    if repo:
        event["repo_name"] = repo
    if _SCOPE != "repo":
        branch = capture._branch_or_none(cwd)
        if branch:
            event["branch_name"] = branch
    repository_id = os.environ.get("VONIC_REPOSITORY_ID", "").strip()
    if repository_id:
        event["repository_id"] = repository_id
    if not event:
        return None
    scope = {"event": event, **_client_identity()}
    if session_id := (session_id or "").strip():
        scope["session_id"] = session_id[:128]
    # `caller_agent` (OpenCode's configured agent name, e.g. "build" vs a dispatched subagent) is a
    # SIBLING of `event`, never a field inside it — the server's recall ledger hashes the event
    # context alone to match a recall against previous ones, so folding caller identity into `event`
    # would change that hash and orphan every prior recall. `RepositoryQueryMetadata` gained the
    # optional `caller_agent` field (server migration 0019_query_caller_agent). Forwarding is DEPLOY-
    # THEN-FLIP gated: an older server without the field rejects any extra key (extra="forbid"), so
    # this stays OFF by default and must be enabled (VONIC_CODECOLLAB_FORWARD_CALLER_AGENT=1) only
    # AFTER the vonic-agent carrying migration 0019 is deployed. Omitted when empty regardless.
    if _FORWARD_CALLER_AGENT and (caller_agent := (caller_agent or "").strip()):
        scope["caller_agent"] = caller_agent[:64]
    return scope


def _client_identity() -> dict:
    """Which tool and build is asking, as siblings of ``event`` on the query metadata.

    NOT inside ``event``. The server hashes the event context to match a recall against previous
    ones, so folding caller identity in there would change that hash the moment this shipped and
    silently orphan every recall recorded before it.

    Both keys are omitted when empty and truncated to the lengths the server accepts. The server
    validates them (min_length=1), so an empty string would fail the whole ``vonic_query`` call —
    and a version label must never be able to break recall itself. The label form matches what the
    capture path sends, so per-tool views agree across the two.
    """
    identity = {}
    tag = capture._client_tag()
    # Known runtime -> its label, so the two families share a vocabulary. Unknown -> the raw tag,
    # which keeps a new runtime visible instead of silently mislabelling it as the default one.
    label = capture._CLIENT_LABELS.get(tag) or tag
    if label:
        identity["client"] = label[:64]
    version = (capture._plugin_version() or "").strip()
    if version:
        identity["client_version"] = version[:32]
    return identity


def _call(prompt_arg: str, scope: dict | None) -> dict:
    """One vonic_query call. `_resolve_becos()` re-runs the token command, so each call mints a
    FRESH short-lived JWT — which is what makes the 401 retry below meaningful."""
    url, token = capture._resolve_becos()
    ident = capture._becos_identity_headers()
    args = {"prompt": prompt_arg}
    if scope:
        args["metadata"] = scope
    return gbrain_client.call_tool(
        url, token, "vonic_query", args, _TIMEOUT, extra_headers=ident,
    )


def _receipt_auth(receipt: gbrain_client.Receipt) -> bool:
    condition = (receipt.condition or "").lower()
    return (receipt.present and receipt.acceptance == "rejected"
            and (condition.startswith("auth") or "unauthorized" in condition
                 or "forbidden" in condition))


def _query(prompt: str, scope: dict | None) -> str:
    """Recall relevant memory for `prompt` via vonic_query ("" when nothing relevant).

    Retries ONCE on a 401 with a freshly minted token: a short-lived JWT can expire between mints,
    and a single re-mint clears that transient auth failure. A second 401 (e.g. the gateway session
    itself expired → needs OTP re-auth) propagates and the hook fails open.
    """
    # A scoped query routes to the repository-memory agent, which wraps the prompt in its own
    # instruction and answers only from stored events — it never "answers the prompt itself", so
    # the recall framing the general-memory path needs would just pollute the search text.
    arg = prompt if scope else _RECALL_INSTRUCTION + prompt
    try:
        result = _call(arg, scope)
        receipt = gbrain_client.result_receipt(result, "vonic_query")
        if _receipt_auth(receipt):
            raise gbrain_client.GbrainError(f"structured auth rejection: {receipt.condition}")
    except gbrain_client.GbrainError as exc:
        if "401" not in str(exc) and "structured auth rejection" not in str(exc):
            raise
        _log(f"retry   401 → re-minting token, retrying vonic_query once  ({exc})")
        try:
            result = _call(arg, scope)  # fresh token via _resolve_becos
        except gbrain_client.GbrainError as exc2:
            # A 401 that survives a fresh mint = expired gateway session, not a stale JWT.
            # Escalate so the hook fails LOUD (banner + notification) instead of silent.
            if "401" in str(exc2):
                raise AuthExpired(str(exc2)) from exc2
            raise
        receipt = gbrain_client.result_receipt(result, "vonic_query")
        if _receipt_auth(receipt):
            raise AuthExpired(f"structured auth rejection: {receipt.condition}")

    receipt = gbrain_client.result_receipt(result, "vonic_query")
    if receipt.present:
        if not receipt.valid:
            _log(f"no-inject  condition={receipt.condition}  detail={receipt.error}")
            return ""
        if not (receipt.acceptance == "accepted" and receipt.state == "terminal"
                and receipt.outcome == "answered"):
            _log("no-inject  acceptance={} state={} outcome={} condition={}".format(
                receipt.acceptance, receipt.state, receipt.outcome, receipt.condition))
            return ""
    return "" if _looks_empty(answer := _answer_text(result)) else answer


def main() -> int:
    if capture.foreign_host():
        # Claude Code hook run by another host (e.g. Cursor's third-party hook import): that host's
        # own CodeCollab plugin owns recall. Nothing is printed, so no context is injected.
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        return 0
    is_claude = capture._client_tag() == "cc"
    source = str(payload.get("recall_source") or
                 ("user-prompt-submit" if is_claude else "unknown")).strip()
    mode = str(payload.get("caller_mode") or ("primary" if is_claude else "unknown")).strip()
    os.environ["VONIC_CODECOLLAB_RECALL_SOURCE"] = (
        source if source in {"tool", "system-transform", "user-prompt-submit"} else "unknown"
    )
    os.environ["VONIC_CODECOLLAB_CALLER_MODE"] = (
        mode if mode in {"primary", "subagent", "all"} else "unknown"
    )
    # UserPromptSubmit is the START boundary of a turn, independent of recall — the marker is
    # stamped before every early return below (slash commands, empty prompts, recall disabled)
    # so `capture.py turn` can scope its repository report to this turn alone.
    try:
        import repo_activity  # noqa: PLC0415 — lazy + optional, same as capture's call site
        repo_activity.mark_turn_start(payload)
    except Exception as exc:  # noqa: BLE001 — never block the prompt
        _debug(f"turn marker: {exc}")

    # Strip harness-injected `<system-reminder>` banners (e.g. Claude Code's per-turn plan-mode
    # notice) before the slash-command check and before anything is sent to vonic_query — this is
    # ephemeral harness control-plane noise, not part of what the user actually asked, and must
    # never reach the backend (dilutes the recall match; would pollute captured memory too).
    prompt = capture.strip_system_reminders(payload.get("prompt") or "")
    if not prompt or prompt.startswith("/"):  # not a model turn — inject nothing at all
        return 0

    # ONE additionalContext payload, assembled from independent parts. Something about citing
    # repositories rides on EVERY model turn, whether or not memory is recalled, so it is added up
    # front, before any of the recall paths below that can bail out — each of those flushes what it
    # has instead of returning silently. Recalled memory, when there is any, is appended after it.
    #   compact (default): one reminder line; the full rules arrive once per session and after
    #                      compaction from `instructions.py session-start`.
    #   legacy:            the full provenance text, as in 0.27.3 (the revert path).
    legacy = provenance.instructions_mode() == "legacy"
    prov_text, feedback_text, resolve_text = provenance.instruction_texts()
    parts: list[str] = []
    if legacy:
        if provenance.is_provenance_enabled():
            parts.append(prov_text)
    elif provenance.session_reminder():
        parts.append(provenance.session_reminder())

    def _flush() -> int:
        if parts:
            _emit_context("\n\n".join(parts))
        return 0

    if os.environ.get("VONIC_RECALL_ENABLED", "1") != "1":
        return _flush()
    prompt = _strip_handoff(prompt)
    if not prompt:  # a handoff with no question after it: nothing to search on
        return _flush()
    cwd = payload.get("cwd") or os.getcwd()   # scopes recall to the checkout being worked in
    scope = _query_scope(
        cwd, payload.get("session_id"), payload.get("caller_agent")
    )                                         # also names the source on the injected wrapper
    if scope is None:
        # Repository memory is the only brain codecollab may read. An unscoped vonic_query
        # routes to the *general* brain, so a cwd that resolves to no repo (or SCOPE=off)
        # skips recall entirely rather than falling back to it.
        _log(f"unscoped  prompt={prompt[:60]!r}  no repository scope — recall skipped")
        return _flush()

    try:
        answer = _query(prompt, scope)
    except AuthExpired as exc:
        # Persistent auth failure: still fail-open (never block the prompt), but LOUDLY.
        # Inject a visible banner (model tells the user) + a rate-limited OS notification,
        # so an expired session can't masquerade as an empty brain.
        _debug(f"auth expired: {exc}")
        _log(f"auth    prompt={prompt[:60]!r}  session expired (persistent 401) — {exc}")
        _notify_auth_expired()
        parts.append(_AUTH_BANNER)
        return _flush()
    except Exception as exc:  # noqa: BLE001 — fail-open (server down, timeout, 503, misconfig)
        _debug(str(exc))
        _log(f"error   prompt={prompt[:60]!r}  {exc}")
        return _flush()
    if not answer:
        _log(f"empty   prompt={prompt[:60]!r}")
        return _flush()
    answer = _truncate_answer(answer, _MAXCHARS)
    snippet = " ".join(answer.split())[:_LOGCHARS]
    _log(f"inject  {len(answer)}c  prompt={prompt[:60]!r}  ::  {snippet}")

    parts.append(_wrap(answer, scope))   # explicit provenance — see _emit_context
    if legacy:
        # Legacy: ask for a relevance/tokens-saved grade, but ONLY here, the one path where memory
        # was actually recalled, after the digest so the thing being graded is in view. Resolve
        # guidance rides the same precondition. In compact mode both are standing rules delivered
        # at session start, worded to apply only to turns that carry a <recalled-memory> block.
        if provenance.is_recall_feedback_enabled():
            parts.append(feedback_text)
        if provenance.is_resolve_tool_enabled():
            parts.append(resolve_text)
    return _flush()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — a recall hook must never break the session
        _debug(f"unexpected: {exc}")
        sys.exit(0)
