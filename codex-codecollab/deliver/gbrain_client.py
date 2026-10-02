#!/usr/bin/env python3
"""Minimal gbrain MCP client (Streamable-HTTP) — `put_page` + `add_timeline_entry`.

`gbrain serve --http` speaks the MCP Streamable-HTTP transport: JSON-RPC POSTed to
`/mcp`, responses as `application/json` or an SSE `text/event-stream`, with a session
established at `initialize` via the `Mcp-Session-Id` header. Auth is a bearer token
(OAuth access token or a `gbrain auth create` token) on every request.

Stdlib only. Each call does the handshake once:
  initialize -> notifications/initialized -> tools/call <tool>
Errors raise GbrainError; the caller logs and drops (never breaks the session).
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "codecollab", "version": "0.4.0"}

_ACCEPTANCE = {"accepted", "rejected"}
_STATES = {"pending", "terminal"}
_OPERATIONS = {"vonic_query", "vonic_remember", "becos_backfill_upload"}
_OUTCOMES = {
    "answered", "no_evidence", "succeeded", "partial", "skipped", "conflict", "failed",
    "unknown",
}
_CONDITIONS = {
    "none", "provider", "backend", "budget", "timeout", "citation", "policy",
    "validation", "auth", "conflict", "write", "internal", "unknown",
}
_RECEIPT_FIELDS = {
    "schema_version", "operation", "request_id", "acceptance", "state", "outcome",
    "condition", "retryable",
}
_AUDIT_SECRET_KEYS = frozenset({
    "authorization", "token", "access_token", "client_secret", "password", "api_key",
})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirects to `_post` so credentials are never forwarded implicitly."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _audit_log_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "codecollab", "backend-api.log")


def _redact_audit_value(value):
    if isinstance(value, dict):
        return {
            key: "***REDACTED***" if key.lower() in _AUDIT_SECRET_KEYS else _redact_audit_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_audit_value(item) for item in value]
    return value


def _audit_call(name: str, arguments: dict, *, response=None, error: Exception | None = None) -> None:
    """Append a complete local request/response audit record when explicitly enabled."""
    if os.environ.get("VONIC_BACKEND_AUDIT_LOG") != "1":
        return
    record = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "client_tag": os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc"),
        "tool": name,
        "request": _redact_audit_value(arguments),
    }
    if source := os.environ.get("VONIC_CODECOLLAB_RECALL_SOURCE"):
        record["recall_source"] = source
    if mode := os.environ.get("VONIC_CODECOLLAB_CALLER_MODE"):
        record["caller_mode"] = mode
    if error is None:
        record["response"] = _redact_audit_value(response)
    else:
        record["error"] = {"type": type(error).__name__, "message": str(error)}
    try:
        os.makedirs(os.path.dirname(_audit_log_path()), exist_ok=True)
        with open(_audit_log_path(), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


class Receipt:
    """Normalized structured receipt. ``present=False`` is the only legacy fallback signal."""

    __slots__ = (
        "present", "valid", "schema_version", "operation", "request_id", "acceptance", "state",
        "outcome", "condition", "retryable", "error",
    )

    def __init__(
        self, *, present: bool, valid: bool, schema_version: str | None = None,
        operation: str | None = None, request_id: str | None = None,
        acceptance: str | None = None, state: str | None = None, outcome: str | None = None,
        condition: str | None = None, retryable: bool = False, error: str | None = None,
    ):
        self.present = present
        self.valid = valid
        self.schema_version = schema_version
        self.operation = operation
        self.request_id = request_id
        self.acceptance = acceptance
        self.state = state
        self.outcome = outcome
        self.condition = condition
        self.retryable = retryable
        self.error = error


class ToolResult(dict):
    """A raw MCP result mapping with its parsed receipt available as ``.receipt``."""

    def __init__(self, raw: dict, receipt: Receipt):
        super().__init__(raw)
        self.receipt = receipt
        self.raw_result = raw


def parse_receipt(result: dict, expected_operation: str) -> Receipt:
    """Strictly parse the flat v1 ``structuredContent`` receipt.

    An explicitly present non-object value is malformed. For objects, either reserved marker,
    ``schema_version`` or ``operation``, identifies the backend's flat taxonomy envelope. Ordinary
    structured tool data (notably backfill ``batch_id/jobs/rollup/distillation``) is legacy payload,
    not a malformed receipt. Once marked, incomplete, malformed, or unsupported taxonomy fails
    closed as explicit retryable ``unknown`` and never falls through to legacy content.
    """
    if not isinstance(result, dict) or "structuredContent" not in result:
        return Receipt(present=False, valid=False)

    raw = result.get("structuredContent")
    if not isinstance(raw, dict):
        return Receipt(
            present=True, valid=False, schema_version="1", operation=expected_operation,
            acceptance="rejected", state="terminal", outcome="unknown",
            condition="unknown", retryable=True,
            error="structuredContent must be an object",
        )
    if not ({"schema_version", "operation"} & set(raw)):
        return Receipt(present=False, valid=False)
    error = None
    if set(raw) != _RECEIPT_FIELDS:
        missing = sorted(_RECEIPT_FIELDS - set(raw))
        extra = sorted(set(raw) - _RECEIPT_FIELDS)
        error = f"receipt fields mismatch (missing={missing}, extra={extra})"
    elif raw.get("schema_version") != "1":
        error = "unsupported receipt schema_version"
    elif raw.get("operation") not in _OPERATIONS:
        error = "unsupported receipt operation"
    elif raw.get("operation") != expected_operation:
        error = "receipt operation mismatch"
    elif not isinstance(raw.get("request_id"), str) or not raw["request_id"].strip():
        error = "invalid receipt request_id"
    elif raw.get("acceptance") not in _ACCEPTANCE:
        error = "invalid receipt acceptance"
    elif raw.get("state") not in _STATES:
        error = "invalid receipt state"
    elif raw.get("outcome") is not None and raw.get("outcome") not in _OUTCOMES:
        error = "invalid receipt outcome"
    elif raw.get("condition") not in _CONDITIONS:
        error = "invalid receipt condition"
    elif type(raw.get("retryable")) is not bool:  # bool only; integers are not accepted
        error = "invalid receipt retryable"
    elif raw["state"] == "pending" and (
        raw["acceptance"] != "accepted" or raw["outcome"] is not None
    ):
        error = "pending receipt must be accepted with null outcome"
    elif raw["state"] == "terminal" and raw["outcome"] is None:
        error = "terminal receipt requires an outcome"
    elif raw["acceptance"] == "rejected" and raw["state"] != "terminal":
        error = "rejected receipt must be terminal"

    if error:
        return Receipt(
            present=True, valid=False, schema_version="1", operation=expected_operation,
            acceptance="rejected", state="terminal", outcome="unknown",
            condition="unknown", retryable=True, error=error,
        )
    return Receipt(
        present=True, valid=True,
        **{key: raw[key] for key in _RECEIPT_FIELDS},
    )


def result_receipt(result: dict, expected_operation: str) -> Receipt:
    """Read an attached receipt when available, otherwise parse a plain/stubbed result."""
    attached = getattr(result, "receipt", None)
    if isinstance(attached, Receipt) and (
        not attached.present or attached.operation == expected_operation
        or (not attached.valid and attached.condition == "unknown")
    ):
        return attached
    return parse_receipt(result, expected_operation)


def tool_data(result: dict) -> dict:
    """Return ordinary structured tool data or the unchanged JSON text payload.

    A marked taxonomy envelope is metadata about the legacy content, not the tool payload itself.
    """
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if (isinstance(structured, dict)
            and not ({"schema_version", "operation"} & set(structured))):
        return structured
    for block in result.get("content", []) if isinstance(result, dict) else []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            decoded = json.loads(block.get("text", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, dict):
            return decoded
    return {}


class GbrainError(Exception):
    """A failed call. ``transport`` separates "could not reach/finish the call" from
    "the server understood and refused it".

    Callers retry on the first kind and stop on the second: a connection failure or timeout says
    nothing about the payload and may succeed unchanged, while a rejection is deterministic and
    will fail identically forever. `capture._deliver_becos` relies on this to tell an outage
    (retry, no budget spent) from a poison turn (park it).
    """

    def __init__(self, message: str, *, transport: bool = False):
        super().__init__(message)
        self.transport = transport


def _parse_body(content_type: str, raw: bytes) -> list[dict]:
    text = raw.decode("utf-8", "replace") if raw else ""
    ctype = (content_type or "").lower()
    messages: list[dict] = []
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    messages.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
    elif text.strip():
        try:
            obj = json.loads(text)
            messages.extend(obj if isinstance(obj, list) else [obj])
        except json.JSONDecodeError:
            pass
    return messages


def _same_origin(source: str, target: str) -> bool:
    """Return whether two absolute HTTP(S) URLs have the same security origin."""
    origins = []
    for url in (source, target):
        try:
            parsed = urllib.parse.urlsplit(url)
            scheme = parsed.scheme.lower()
            hostname = parsed.hostname
            port = parsed.port
        except ValueError:
            return False
        if scheme not in ("http", "https") or not hostname:
            return False
        if port is None:
            port = 443 if scheme == "https" else 80
        origins.append((scheme, hostname.lower(), port))
    return origins[0] == origins[1]


def _post(
    url: str,
    token: str,
    session_id: str | None,
    message: dict,
    timeout: float,
    extra_headers: dict | None = None,
    _redirects: int = 2,
):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url, data=json.dumps(message).encode("utf-8"), method="POST", headers=headers
    )
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
            sid = resp.headers.get("Mcp-Session-Id") or session_id
            return _parse_body(resp.headers.get("Content-Type", ""), raw), sid
    except urllib.error.HTTPError as exc:
        # Re-POST redirects only after validating their destination. The no-redirect opener above
        # also exposes 301/302/303 here instead of letting urllib forward credentials implicitly.
        # A 303 changes the request to GET by definition, which this POST-only MCP transport does
        # not implement. Replaying the body would duplicate a potentially side-effecting call.
        if exc.code in (301, 302, 307, 308) and _redirects > 0:
            location = exc.headers.get("Location")
            if location:
                target = urllib.parse.urljoin(url, location)
                if _same_origin(url, target):
                    return _post(target, token, session_id, message, timeout,
                                 extra_headers=extra_headers, _redirects=_redirects - 1)
        raise


def call_tool(
    url: str,
    token: str,
    name: str,
    arguments: dict,
    timeout: float = 15.0,
    extra_headers: dict | None = None,
) -> dict:
    """Call one MCP tool. Raises GbrainError on any failure.

    ``extra_headers`` are sent on every request (e.g. X-Tenant-ID / X-User-ID identity
    for the becos surface).
    """
    if not url:
        raise GbrainError("no gbrain url")
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
    }
    try:
        _messages, session_id = _post(url, token, None, init, timeout, extra_headers)
        try:
            _post(url, token, session_id, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                  timeout, extra_headers)
        except urllib.error.HTTPError as exc:
            if exc.code not in (200, 202, 204):
                raise
        call = {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        messages, _session_id = _post(url, token, session_id, call, timeout, extra_headers)
    except urllib.error.HTTPError as exc:
        # 5xx/408/429 are the server saying "not now"; other 4xx are a verdict on the request.
        retryable = exc.code >= 500 or exc.code in (408, 429)
        error = GbrainError(f"http {exc.code}: {exc.reason}", transport=retryable)
        _audit_call(name, arguments, error=error)
        raise error from exc
    except (urllib.error.URLError, OSError) as exc:
        # Includes socket timeouts: the call may well have landed, we just never read the reply.
        error = GbrainError(str(exc), transport=True)
        _audit_call(name, arguments, error=error)
        raise error from exc

    resp = next((m for m in messages if m.get("id") == 2), None)
    if resp is None:
        # No reply to read is not a verdict on the payload — the call may well have landed, same as
        # a timeout. Retryable, so a truncated stream never gets a turn written off as poison.
        error = GbrainError(f"no response to {name}", transport=True)
        _audit_call(name, arguments, error=error)
        raise error
    if resp.get("error"):
        error = GbrainError(f"{name} rejected: {resp['error']}")
        _audit_call(name, arguments, response=resp, error=error)
        raise error
    raw_result = resp.get("result", {})
    if not isinstance(raw_result, dict):
        error = GbrainError(f"{name} returned a non-object result")
        _audit_call(name, arguments, response=resp, error=error)
        raise error
    result = ToolResult(raw_result, parse_receipt(raw_result, name))
    # A complete structured rejection is a valid wire response that callers classify by receipt.
    # Receipt-less and malformed isError results retain the legacy raised-error contract.
    if result.get("isError") and not (result.receipt.present and result.receipt.valid):
        error = GbrainError(f"{name} tool error: {result}")
        _audit_call(name, arguments, response=raw_result, error=error)
        raise error
    _audit_call(name, arguments, response=raw_result)
    return result


def put_page(url: str, token: str, slug: str, content: str, timeout: float = 15.0) -> dict:
    return call_tool(url, token, "put_page", {"slug": slug, "content": content}, timeout)


def add_timeline_entry(
    url: str, token: str, slug: str, date: str, summary: str, detail: str = "",
    source: str = "", timeout: float = 15.0,
) -> dict:
    # gbrain requires date as YYYY-MM-DD. Entries dedup on (page, date, summary,
    # source), so callers vary `source` to keep same-day/same-summary turns distinct.
    args = {"slug": slug, "date": date, "summary": summary}
    if detail:
        args["detail"] = detail
    if source:
        args["source"] = source
    return call_tool(url, token, "add_timeline_entry", args, timeout)
