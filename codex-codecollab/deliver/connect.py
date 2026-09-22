#!/usr/bin/env python3
"""Authenticate codecollab to the gateway with browser authorization and PKCE.

Usage:
  python3 connect.py start [--gateway https://gateway.example.com]
  python3 connect.py complete <authorization-code>

The browser flow is noninteractive: ``start`` prints the URL to open and
``complete`` redeems the UUID copied from that page. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


_AUTH_FILE = "gateway-auth.json"
_PENDING_FILE = "gateway-auth-pending.json"
# Hosted gateway, so `/codecollab:login` needs no argument and no environment — which was the
# entire point of making capture.py resolve one. Duplicated from capture.py's _DEFAULT_GATEWAY
# rather than imported: connect.py is stdlib-only by design, and importing capture would drag
# the whole delivery module (gbrain_client, rest_client, fcntl) into the login script.
# test_connect asserts the two stay equal. They drifted once already, and the result was that
# login kept demanding a gateway long after capture.py had stopped needing one.
_DEFAULT_GATEWAY = "https://becos.ai"


class ConnectError(Exception):
    """A safe-to-display authentication error."""


def _cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(base, "codecollab")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _path(name: str) -> str:
    return os.path.join(_cache_dir(), name)


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ConnectError(f"could not read {os.path.basename(path)}") from exc
    if not isinstance(value, dict):
        raise ConnectError(f"invalid {os.path.basename(path)}")
    return value


def _atomic_write(path: str, payload: dict) -> None:
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            json.dump(payload, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise ConnectError(f"gateway rejected the request (HTTP {exc.code})") from exc
    except urllib.error.URLError as exc:
        raise ConnectError("could not reach the gateway") from exc
    except (OSError, ValueError) as exc:
        raise ConnectError("invalid response from the gateway") from exc
    if not isinstance(result, dict):
        raise ConnectError("invalid response from the gateway")
    return result


def _cached_gateway() -> str:
    try:
        value = _load_json(_path(_AUTH_FILE)).get("gateway", "")
    except ConnectError:
        return ""
    return str(value).strip().rstrip("/")


def _resolve_gateway(argument: str | None) -> str:
    gateway = (argument or os.environ.get("VONIC_GATEWAY_URL") or _cached_gateway()
               or _DEFAULT_GATEWAY).strip()
    gateway = gateway.rstrip("/")
    if not gateway:
        # Only reachable if _DEFAULT_GATEWAY is emptied; kept so that cannot fail silently.
        raise ConnectError("no gateway configured; pass --gateway or set VONIC_GATEWAY_URL")
    _validate_transport_url(gateway, "gateway")
    return gateway


def _validate_transport_url(value: str, label: str) -> None:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise ConnectError(f"{label} returned an invalid URL") from exc
    host = parsed.hostname or ""
    try:
        loopback = host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise ConnectError(f"{label} returned an invalid URL")
    if parsed.scheme != "https" and not loopback:
        raise ConnectError(f"{label} must use HTTPS except on loopback")


def _pkce_pair() -> tuple[str, str]:
    # 64 random bytes encode to 86 RFC 7636 unreserved characters (valid range: 43-128).
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def start(gateway_argument: str | None) -> None:
    gateway = _resolve_gateway(gateway_argument)
    verifier, challenge = _pkce_pair()
    result = _post(
        f"{gateway}/auth/codecollab/authorizations",
        {"code_challenge": challenge},
    )
    try:
        request_id = str(uuid.UUID(str(result["request_id"])))
        authorization_url = str(result["authorization_url"])
        expires_in = int(result["expires_in"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConnectError("gateway returned an invalid authorization response") from exc
    _validate_transport_url(authorization_url, "authorization page")
    if expires_in <= 0:
        raise ConnectError("gateway returned an invalid authorization response")
    _atomic_write(_path(_PENDING_FILE), {
        "request_id": request_id,
        "code_verifier": verifier,
        "expires_at": time.time() + expires_in,
        "gateway": gateway,
    })
    print("Open this URL in a browser and finish signing in:")
    print(authorization_url)
    # Runtime-agnostic: the login command differs per client (Claude Code /codecollab:login,
    # opencode /login, codex `codecollab-cx login`), so name none of them here.
    print("Then finish signing in by running the login command again with the UUID it shows.")


def complete(code_text: str) -> None:
    try:
        code = str(uuid.UUID(code_text))
    except (ValueError, AttributeError) as exc:
        raise ConnectError("authorization code must be a UUID") from exc

    pending_path = _path(_PENDING_FILE)
    pending = _load_json(pending_path)
    try:
        _request_id = str(uuid.UUID(str(pending["request_id"])))
        verifier = str(pending["code_verifier"])
        expires_at = float(pending["expires_at"])
        gateway = str(pending["gateway"]).rstrip("/")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ConnectError("invalid pending authorization; start login again") from exc
    if time.time() >= expires_at:
        raise ConnectError("pending authorization expired; start login again")
    try:
        _validate_transport_url(gateway, "pending gateway")
    except ConnectError as exc:
        raise ConnectError("invalid pending authorization; start login again") from exc

    result = _post(
        f"{gateway}/auth/codecollab/token",
        {"code": code, "code_verifier": verifier},
    )
    token = result.get("token")
    tenant_id = result.get("tenant_id")
    if not isinstance(token, str) or not token or not isinstance(tenant_id, str) or not tenant_id:
        raise ConnectError("gateway returned an invalid token response")

    # Validate everything before the replace. Any failure above preserves existing credentials.
    _atomic_write(_path(_AUTH_FILE), {
        "token": token,
        "gateway": gateway,
        "tenant_id": tenant_id,
        # The gateway names the user in its token response; we printed it and dropped it.
        # capture.py uses it as a last-resort author so an install with no git identity
        # does not ship author_name=None (which the server drops). Optional: absent on a
        # gateway that returns neither field, and capture.py handles that.
        "user_name": (result.get("user_name") or result.get("user_id") or None),
    })
    try:
        os.unlink(pending_path)
    except FileNotFoundError:
        pass
    except OSError:
        # Authentication succeeded and the credential is durable. A stale pending file is
        # harmless and must not turn that success into a reported failure.
        print("Warning: could not remove stale pending authorization state.", file=sys.stderr)
    print(f"Connected as {result.get('user_name') or result.get('user_id') or 'a gateway user'}.")
    print("CodeCollab authentication is ready.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Connect codecollab to the gateway.")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    start_parser = subparsers.add_parser("start", help="start browser authorization")
    start_parser.add_argument("--gateway", help="gateway base URL")
    complete_parser = subparsers.add_parser("complete", help="redeem the browser authorization code")
    complete_parser.add_argument("code", help="UUID shown after browser authorization")
    args = parser.parse_args(argv)
    try:
        if args.operation == "start":
            start(args.gateway)
        else:
            complete(args.code)
    except ConnectError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 1
    except OSError:
        print("Authentication failed: could not update the local credential cache", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
