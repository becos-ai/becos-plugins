#!/usr/bin/env python3
"""POST one captured message to a custom REST ingest endpoint (VONIC_LOG_URL).

Contract (per message):
  POST <url>
  Content-Type: application/json
  Authorization: Bearer <VONIC_LOG_TOKEN>
  Idempotency-Key: <uuid>   # stable across retries for the same message
  body: {timestamp, role, text, repositoryName?, branch?, commitId?}
        # + sessionId, turnIndex unless VONIC_LOG_LINK_FIELDS=0 (link a prompt to its
        #   answer). The full key set is frozen in contracts/coding-agent-message.json:
        #   receivers may set extra="forbid", where one unknown key 422s the message.

Stdlib only. Raises RestError on any failure; the caller keeps the message
un-sent (its Idempotency-Key preserved) and retries later.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class RestError(Exception):
    pass


def post_message(url: str, token: str, payload: dict, idempotency_key: str, timeout: float = 15.0) -> int:
    if not url:
        raise RestError("no rest url")
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        raise RestError(f"http {exc.code}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise RestError(str(exc)) from exc
