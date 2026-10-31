#!/usr/bin/env python3
"""Becos project repositories: a read-only local mirror of each nocode project's compiled folder.

    python3 project_repo.py sync [--cwd DIR] [--session ID] [--force]   download or update (detached)
    python3 project_repo.py status [--cwd DIR] [--session ID]           path + last sync, as JSON
    python3 project_repo.py context [--cwd DIR] [--session ID]          {"text"}: the session block,
                                                                        and a detached sync if due
    python3 project_repo.py session-end                                 SessionEnd hook (stdin JSON)
    python3 project_repo.py prompt-submit                               per-prompt hook (stdin JSON)
    python3 project_repo.py pre-tool-use                                Claude Code PreToolUse hook

vonic-stack ``docs/PLAN_becos_project_repo.md``. The Becos server compiles every member's work in a
nocode project into a repository (digests in ``sessions/``, live documents: strategy, roadmap,
TODOs, features, decisions, rejected ideas, failed approaches). This module keeps a copy at
``~/.becos/projects/<tenant>/<project>/`` (``VONIC_CODECOLLAB_PROJECTS_DIR`` moves it) so the agent
can grep and read it like any files:

* Only the server writes (D1). Plain HTTPS with the standard library, no git on this machine
  (§15): a manifest of the server's current tree (path, git blob id, size per file), then a tar of
  just the files whose content differs. Every downloaded file is checked against its blob id
  before it replaces anything. Local edits are overwritten and stray files removed (P9), so a
  server-side purge converges on the next sync.
* ``.becos-mirror.json`` in the folder marks it as this module's and records the head it holds
  plus each file's size, mtime and blob id, so an unchanged folder costs one 304 and a changed
  file is the only one re-hashed.
* Download when a session starts in a nocode project (or one is chosen or created), then update at
  most every ``VONIC_CODECOLLAB_PROJECT_REPO_INTERVAL`` seconds (default 300) while the session is
  active. Always in a detached child: a hook never waits on the network.
* The short-lived becos token the MCP calls use goes in the Authorization header only; it is never
  written to disk or logged.
* A non-blocking ``flock`` per repo: a second session on the machine skips that round.
* A failure is logged in debug, recorded for the session context, and never raised into a hook.
* A mirror whose project is gone from the team's project list is deleted (:func:`prune`): checked
  at most every 6 hours from the background sync, and at once when the server reports the
  project's repository missing. Only folders this module wrote are touched.

On by default; ``VONIC_CODECOLLAB_PROJECT_REPO=0`` opts out.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — becos config, token, debug log
import connect  # noqa: E402 — atomic writes
import project  # noqa: E402 — the session's project

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OID_RE = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_INTERVAL = 300
_HTTP_TIMEOUT = 120
MARKER = ".becos-mirror.json"
# Above this many changed files the whole tree is requested instead of a path list.
_MAX_PATHS = 500
LIVE_DOCS = ("product-strategy.md", "roadmap.md", "todos.md", "features/", "decisions/",
             "rejected-ideas.md", "failed-approaches.md", "files/")
# The Claude Code tools that only read; the PreToolUse hook allows these inside the mirror root.
_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS"})
_PRUNE_EVERY = 6 * 3600
# The server lists at most this many projects; a listing this long may be cut off, so no prune.
_LIST_CEILING = 500
# Reasons recorded for the session context, in words the agent can pass on.
_REASONS = {"network": "the Becos server could not be reached",
            "auth": "the sign-in was rejected; /becos:login fixes it",
            "forbidden": "this account may not read the project",
            "server": "the Becos server returned an error",
            "integrity": "the download did not match the server's listing",
            "local": "the folder could not be written",
            "missing": "the Becos server has no folder for it right now"}


def enabled() -> bool:
    """Default on, like projects: an explicit value must be ``1`` (trimmed) to enable it."""
    return (os.environ.get("VONIC_CODECOLLAB_PROJECT_REPO", "1").strip() == "1"
            and project.enabled() and os.environ.get("VONIC_CODECOLLAB_DISABLED") != "1")


def root() -> str:
    configured = os.environ.get("VONIC_CODECOLLAB_PROJECTS_DIR", "").strip()
    return os.path.abspath(os.path.expanduser(configured or "~/.becos/projects"))


def _interval() -> int:
    try:
        return max(30, int(os.environ.get("VONIC_CODECOLLAB_PROJECT_REPO_INTERVAL", "")))
    except ValueError:
        return _DEFAULT_INTERVAL


def _state_path() -> str:
    return os.path.join(capture._cache_dir(), "project-repo.json")


def _state() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(update: dict) -> None:
    try:
        connect._atomic_write(_state_path(), {**_state(), **update})
    except OSError as exc:
        capture._debug(f"project-repo: state write failed: {exc}")


def _state_map(key: str) -> dict:
    value = _state().get(key)
    return value if isinstance(value, dict) else {}


def _gateway_host() -> str:
    return capture._url_host(capture._resolve_gateway())


def token_tenant(token: str) -> str:
    """The ``org_id`` claim of a becos JWT (no verification: it only names a folder and the
    URL; the server authorises every request)."""
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return ""
    tenant = str(claims.get("org_id") or "") if isinstance(claims, dict) else ""
    return tenant if _ID_RE.match(tenant) else ""


def cached_tenant() -> str:
    """The tenant learned at the last sync against this gateway (local: session start uses it)."""
    tenant = _state_map("tenants").get(_gateway_host(), "")
    return tenant if isinstance(tenant, str) and _ID_RE.match(tenant) else ""


def nocode_name(cwd: str | None, session_id: object) -> str | None:
    """The session's nocode project name, or None (a git repo, or no project yet)."""
    resolved = project.resolve(cwd, session_id)
    if resolved is None or resolved.type != project.TYPE_NOCODE:
        return None
    name = resolved.key[len(project.NOCODE_PREFIX):] if resolved.key.startswith(
        project.NOCODE_PREFIX) else resolved.name
    return name if project.valid_name(name) else None


def local_path(tenant: str, name: str) -> str:
    if not (_ID_RE.match(tenant or "") and project.valid_name(name or "")):
        raise ValueError("invalid tenant or project name")
    return os.path.join(root(), tenant, name)


def remote_url(tenant: str, name: str) -> str:
    return f"{capture._resolve_gateway().rstrip('/')}/git/{tenant}/{name}.git"


# --- the local folder -----------------------------------------------------------------------------

def _read_marker(path: str) -> dict | None:
    try:
        with open(os.path.join(path, MARKER), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def present(path: str) -> bool:
    """The folder holds a completed download (its marker names a head)."""
    marker = _read_marker(path)
    return bool(marker and isinstance(marker.get("head"), str) and marker["head"])


def _legacy_clone(path: str, tenant: str, name: str) -> bool:
    """A folder an earlier release cloned with git: its .git/config names this project's URL.
    Read as text, so no git is needed to recognise it."""
    try:
        with open(os.path.join(path, ".git", "config"), encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.strip().startswith("url") and line.strip().endswith(f"/git/{tenant}/{name}.git")
               for line in text.splitlines())


def owned(path: str, tenant: str, name: str) -> bool:
    """``path`` is a mirror this module wrote (never touch anything else)."""
    if os.path.islink(path) or not os.path.isdir(path):
        return False
    marker = _read_marker(path)
    if marker is not None:
        return marker.get("tenant") == tenant and marker.get("name") == name
    return _legacy_clone(path, tenant, name) or not os.listdir(path)


def blob_id(path: str) -> str:
    """git's blob id of a file: sha1 of ``blob <size>\\0`` and the bytes (no git needed)."""
    digest = hashlib.sha1(f"blob {os.path.getsize(path)}\0".encode())  # noqa: S324 — git's id
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_rel(rel: object) -> bool:
    if not isinstance(rel, str) or not rel or len(rel) > 1024 or "\0" in rel or "\\" in rel:
        return False
    parts = rel.split("/")
    return (rel != MARKER and not rel.startswith("/")
            and all(p not in ("", ".", "..") and p.lower() != ".git" for p in parts))


def _scan(path: str) -> tuple[dict[str, os.stat_result], list[str]]:
    """Regular files under ``path`` (relative, ``/``-separated) with their stat, and every other
    entry that must go (symlinks, sockets, a leftover ``.git``). Symlinks are never followed."""
    files: dict[str, os.stat_result] = {}
    odd: list[str] = []
    if not os.path.isdir(path):
        return files, odd
    for current, dirs, names in os.walk(path, followlinks=False):
        rel_dir = os.path.relpath(current, path).replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        for d in list(dirs):
            if os.path.islink(os.path.join(current, d)) or (not prefix and d == ".git"):
                odd.append(prefix + d)
                dirs.remove(d)
        for n in names:
            rel = prefix + n
            if not prefix and n == MARKER:
                continue
            st = os.lstat(os.path.join(current, n))
            if stat.S_ISREG(st.st_mode):
                files[rel] = st
            else:
                odd.append(rel)
    return files, odd


# --- HTTP -----------------------------------------------------------------------------------------

class SyncError(RuntimeError):
    def __init__(self, category: str, detail: str = "", status: int | None = None) -> None:
        super().__init__(f"{category}{f' {status}' if status else ''}{f': {detail}' if detail else ''}")
        self.category, self.status = category, status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to wherever it points: refuse it."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D102
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(url: str, token: str, *, body: dict | None = None, etag: str = ""):
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "becos-project-sync/1"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if etag:
        headers["If-None-Match"] = f'"{etag}"'
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    try:
        return 200, _OPENER.open(req, timeout=_HTTP_TIMEOUT)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (304, 404, 409):
            return exc.code, None
        category = {401: "auth", 403: "forbidden"}.get(exc.code, "server")
        raise SyncError(category, status=exc.code) from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SyncError("network", _redact(str(getattr(exc, "reason", exc)))) from None


def _redact(text: str) -> str:
    # Never echo anything token-shaped back into a log.
    return re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<token>", text or "")[:300]


def _manifest(base: str, token: str, etag: str) -> tuple[int, dict | None]:
    status, resp = _http(f"{base}/becos/manifest", token, etag=etag)
    if resp is None:
        return status, None
    try:
        with resp:
            data = json.loads(resp.read())
        files = data["files"]
        if (not isinstance(data.get("head"), str) or not _OID_RE.match(data["head"])
                or not isinstance(files, list)):
            raise ValueError
        listing = {}
        for f in files:
            if not (isinstance(f, dict) and _OID_RE.match(str(f.get("oid")))
                    and isinstance(f.get("size"), int) and f["size"] >= 0):
                raise ValueError
            listing[f["path"]] = {"oid": f["oid"], "size": f["size"],
                                  "exec": f.get("mode") == "100755"}
    except (OSError, ValueError, KeyError, TypeError):
        raise SyncError("server", "unreadable manifest") from None
    return 200, {"head": data["head"], "files": listing}


def _download(base: str, token: str, head: str, needed: dict[str, dict], whole: bool,
              staging: str) -> dict[str, str] | None:
    """Fetch ``needed`` into ``staging``, each file checked against its size and blob id.
    Returns {relative path: staged file}, or None when main moved (409)."""
    paths = [] if whole else sorted(needed)
    status, resp = _http(f"{base}/becos/archive", token, body={"head": head, "paths": paths})
    if resp is None:
        if status == 409:
            return None
        raise SyncError("server", status=status)
    staged: dict[str, str] = {}
    try:
        with resp, tarfile.open(fileobj=resp, mode="r|gz") as tar:
            for member in tar:
                if member.name not in needed:
                    if whole and member.isreg():
                        continue          # the whole tree came back; this file is already right
                    raise SyncError("integrity", "unexpected entry")
                want = needed[member.name]
                if not member.isreg() or member.size != want["size"] or member.name in staged:
                    raise SyncError("integrity", "entry does not match the listing")
                source = tar.extractfile(member)
                digest = hashlib.sha1(f"blob {member.size}\0".encode())  # noqa: S324 — git's id
                target = os.path.join(staging, str(len(staged)))
                with open(target, "wb") as out:
                    for chunk in iter(lambda: source.read(1 << 20), b""):
                        digest.update(chunk)
                        out.write(chunk)
                if digest.hexdigest() != want["oid"]:
                    raise SyncError("integrity", "content does not match its id")
                staged[member.name] = target
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise SyncError("network" if isinstance(exc, OSError) else "integrity",
                        type(exc).__name__) from None
    if set(staged) != set(needed):
        raise SyncError("integrity", "download incomplete")
    return staged


# --- sync -----------------------------------------------------------------------------------------

def _lock(path: str):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fh = open(path, "a")  # noqa: SIM115 — held for the sync, closed by the caller
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _place(path: str, rel: str) -> str:
    """The absolute target for ``rel``, with every parent a real directory inside ``path``
    (a file or symlink in the way is removed) and nothing standing at the target itself."""
    parts = rel.split("/")
    current = path
    for part in parts[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current) or (os.path.lexists(current) and not os.path.isdir(current)):
            os.unlink(current)
        os.makedirs(current, exist_ok=True)
    target = os.path.join(current, parts[-1])
    base, parent = os.path.realpath(path), os.path.realpath(os.path.dirname(target))
    if parent != base and not parent.startswith(base + os.sep):
        raise SyncError("integrity", "path escapes the folder")
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    return target


def _prune_empty_dirs(path: str) -> None:
    for current, dirs, files in os.walk(path, topdown=False):
        if current != path and not dirs and not files:
            try:
                os.rmdir(current)
            except OSError:
                pass


def _converge(path: str, tenant: str, name: str, base: str, token: str,
              marker: dict | None) -> dict:
    """Make ``path`` equal the server's current tree. One retry when a compile moves main between
    the manifest and the download."""
    index = (marker or {}).get("index") if isinstance((marker or {}).get("index"), dict) else {}
    for _attempt in range(2):
        disk, odd = _scan(path)
        dirty = bool(odd) or set(disk) != set(index) or any(
            not isinstance(index.get(rel), list) or index[rel][:2] != [st.st_size, st.st_mtime_ns]
            for rel, st in disk.items())
        etag = "" if dirty or not marker else str(marker.get("head") or "")
        status, listing = _manifest(base, token, etag)
        if status == 304:
            return {"status": "unchanged", "path": path, "head": etag}
        if status == 404:
            raise SyncError("missing", status=404)
        files, seen, skipped = {}, set(), 0
        fold = sys.platform in ("darwin", "win32")      # case-insensitive by default there
        for rel, entry in listing["files"].items():
            key = rel.lower() if fold else rel
            if not _safe_rel(rel) or key in seen:
                skipped += 1
                continue
            seen.add(key)
            files[rel] = entry
        if skipped:
            capture._debug(f"project-repo: skipped {skipped} unsafe or colliding path(s)")
        new_index, needed = {}, {}
        for rel, entry in files.items():
            st = disk.get(rel)
            if st is None:
                needed[rel] = entry
                continue
            known = index.get(rel)
            same_stat = isinstance(known, list) and known[:2] == [st.st_size, st.st_mtime_ns]
            oid = known[2] if same_stat and len(known) > 2 else blob_id(os.path.join(path, rel))
            if oid == entry["oid"]:
                new_index[rel] = [st.st_size, st.st_mtime_ns, oid]
            else:
                needed[rel] = entry
        extra = sorted(set(disk) - set(files)) + odd
        staged: dict[str, str] = {}
        staging = None
        try:
            if needed:
                os.makedirs(os.path.join(root(), ".staging"), mode=0o700, exist_ok=True)
                staging = tempfile.mkdtemp(prefix=f"{tenant}-{name}-",
                                           dir=os.path.join(root(), ".staging"))
                result = _download(base, token, listing["head"], needed,
                                   len(needed) > _MAX_PATHS, staging)
                if result is None:
                    continue                                       # 409: main moved, start over
                staged = result
            try:
                os.makedirs(path, exist_ok=True)
                if marker is None:
                    # Claim the folder before writing into it, so an interrupted first download
                    # is still recognised as ours (no head: not yet present).
                    connect._atomic_write(os.path.join(path, MARKER), {
                        "version": 1, "gateway": _gateway_host(), "tenant": tenant,
                        "name": name, "head": "", "index": {}})
                for rel in extra:
                    full = os.path.join(path, rel)
                    if os.path.isdir(full) and not os.path.islink(full):
                        shutil.rmtree(full)
                    elif os.path.lexists(full):
                        os.unlink(full)
                for rel, source in staged.items():
                    target = _place(path, rel)
                    os.chmod(source, 0o755 if files[rel]["exec"] else 0o644)
                    os.replace(source, target)
                    st = os.stat(target)
                    new_index[rel] = [st.st_size, st.st_mtime_ns, files[rel]["oid"]]
                _prune_empty_dirs(path)
                connect._atomic_write(os.path.join(path, MARKER), {
                    "version": 1, "gateway": _gateway_host(), "tenant": tenant, "name": name,
                    "head": listing["head"], "index": new_index})
            except OSError as exc:
                raise SyncError("local", type(exc).__name__) from None
        finally:
            if staging:
                shutil.rmtree(staging, ignore_errors=True)
        changed = sum(1 for rel in staged if rel in disk)
        return {"status": "synced", "path": path, "head": listing["head"],
                "added": len(staged) - changed, "changed": changed,
                "deleted": len(set(disk) - set(files)),
                "bytes": sum(files[rel]["size"] for rel in staged)}
    raise SyncError("server", "the project kept changing during the download")


def sync(name: str, *, force: bool = False) -> dict:
    """Download or update one project's mirror. Returns a status dict; raises SyncError on a
    failure worth logging (and records it for :func:`context`). Skips (``skipped``) when another
    process holds the lock or the last sync was under the interval ago (unless ``force``)."""
    url, token = capture._resolve_becos()
    tenant = token_tenant(token)
    if not tenant:
        raise SyncError("auth", "the becos token names no team")
    _save_state({"tenants": {**_state_map("tenants"), _gateway_host(): tenant}})
    path = local_path(tenant, name)
    have = present(path)
    last = _state_map("synced").get(path, 0)
    if not force and have and time.time() - float(last or 0) < _interval():
        return {"status": "skipped", "reason": "recent", "path": path}
    lock = _lock(os.path.join(root(), ".locks", f"{tenant}-{name}.lock"))
    if lock is None:
        return {"status": "skipped", "reason": "locked", "path": path}
    try:
        marker = _read_marker(path)
        if os.path.lexists(path) and not owned(path, tenant, name):
            raise SyncError("local", "the folder exists and was not written by becos")
        if marker is None and os.path.isdir(os.path.join(path, ".git")):
            shutil.rmtree(os.path.join(path, ".git"))    # an earlier git clone: convert in place
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try:
            result = _converge(path, tenant, name, remote_url(tenant, name), token, marker)
        except SyncError as exc:
            if exc.category == "missing" and os.path.isdir(path):
                # The project may have been deleted: check the listing now, not in 6 hours.
                lock.close()             # prune takes this repo's lock itself
                if path in prune(tenant, keep=(), force=True):
                    return {"status": "removed", "path": path}
            raise
    except SyncError as exc:
        _save_state({"errors": {**_state_map("errors"),
                                path: {"at": time.time(), "reason": exc.category}}})
        exc.path = path
        raise
    finally:
        lock.close()
    _save_state({"synced": {**_state_map("synced"), path: time.time()},
                 "errors": {k: v for k, v in _state_map("errors").items() if k != path}})
    return result


def listed_names() -> set[str] | None:
    """Every nocode project name in the caller's team, fresh from the server; None when the list
    can't be trusted (a failure, or a listing long enough to be cut off)."""
    try:
        data = project.call({"action": "list"})
    except Exception as exc:  # noqa: BLE001 — no listing, no prune
        capture._debug(f"project-repo: listing failed: {type(exc).__name__}")
        return None
    projects = data.get("projects")
    if not isinstance(projects, list) or len(projects) >= _LIST_CEILING:
        return None
    names = set()
    for entry in projects:
        key = entry.get("key") if isinstance(entry, dict) else None
        if isinstance(key, str) and key.startswith(project.NOCODE_PREFIX):
            names.add(key[len(project.NOCODE_PREFIX):])
    return names


def prune(tenant: str, *, keep: tuple[str, ...] = (), force: bool = False,
          names: set[str] | None = None) -> list[str]:
    """Delete the mirrors under ``<root>/<tenant>/`` whose project is no longer in the team's list
    (a deleted project). Rate-limited to every 6 hours unless ``force``; ``keep`` names are never
    removed (the session's own project). Returns the removed paths."""
    base = os.path.join(root(), tenant) if _ID_RE.match(tenant or "") else ""
    if not base or not os.path.isdir(base):
        return []
    pruned_at = _state_map("pruned").get(tenant, 0)
    if not force and time.time() - float(pruned_at or 0) < _PRUNE_EVERY:
        return []
    listed = listed_names() if names is None else names
    if listed is None:
        return []
    _save_state({"pruned": {**_state_map("pruned"), tenant: time.time()}})
    removed = []
    for entry in sorted(os.listdir(base)):
        path = os.path.join(base, entry)
        if (entry in listed or entry in keep or not project.valid_name(entry)
                or not owned(path, tenant, entry)):
            continue
        lock = _lock(os.path.join(root(), ".locks", f"{tenant}-{entry}.lock"))
        if lock is None:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            capture._debug(f"project-repo: remove failed: {exc}")
            continue
        finally:
            lock.close()
        removed.append(path)
        capture._debug(f"project-repo: removed {path} (project no longer exists)")
    if removed:
        _save_state({"synced": {k: v for k, v in _state_map("synced").items()
                                if k not in removed},
                     "errors": {k: v for k, v in _state_map("errors").items()
                                if k not in removed}})
    return removed


def spawn_sync(cwd: str | None, session_id: object, *, force: bool = False) -> bool:
    """Run :func:`sync` for the session's project in a detached child (hooks never wait)."""
    if not enabled() or nocode_name(cwd, session_id) is None:
        return False
    args = [sys.executable, os.path.abspath(__file__), "sync", "--cwd", cwd or os.getcwd()]
    if isinstance(session_id, str) and session_id:
        args += ["--session", session_id]
    if force:
        args.append("--force")
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        return False
    return True


def due(cwd: str | None, session_id: object) -> bool:
    """A periodic sync is due for this session's project (local check, no network)."""
    name = nocode_name(cwd, session_id)
    tenant = cached_tenant()
    if name is None:
        return False
    if not tenant:
        return True
    try:
        path = local_path(tenant, name)
    except ValueError:
        return True
    if not present(path):
        return True
    last = _state_map("synced").get(path, 0)
    return time.time() - float(last or 0) >= _interval()


# --- context for the agent ----------------------------------------------------------------------

def _when(at: object) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(at)))
    except (TypeError, ValueError):
        return "an unknown time"


def context(cwd: str | None, session_id: object) -> str:
    """The session-start block naming the mirror, its documents and the precedence rule; "" when
    off, the session isn't in a nocode project, or the server has no folder for it. Local only."""
    if not enabled():
        return ""
    name = nocode_name(cwd, session_id)
    if name is None:
        return ""
    tenant = cached_tenant()
    path = local_path(tenant, name) if tenant else os.path.join(root(), "<team>", name)
    have = present(path)
    error = _state_map("errors").get(path)
    error = error if isinstance(error, dict) else None
    reason = _REASONS.get(str((error or {}).get("reason")), "")
    if error and error.get("reason") == "missing" and not have:
        return ""                     # the server keeps no folder for this project (feature off)
    if not have and error:
        return "\n".join([
            "<becos-project-repo>",
            f"This session's Becos project `{name}` has a compiled folder on the Becos server, "
            f"but it is not on this machine yet: the last download failed at "
            f"{_when(error.get('at'))} ({reason}). It is retried in the background. Until it is "
            f"at `{path}`, answer from Becos memory instead of that folder.",
            "</becos-project-repo>"])
    where = f"`{path}`" if have else (
        f"`{path}` (downloading in the background; if it is not there yet when you need it, say "
        "so instead of guessing its contents)")
    lines = [
        "<becos-project-repo>",
        f"This session's Becos project `{name}` has a compiled, read-only folder on this machine: "
        f"{where}.",
        "It holds what the team worked out in this project, written by the Becos server from "
        "every member's sessions: README.md (index), product-strategy.md, roadmap.md, todos.md, "
        "features/<feature>.md, decisions/, rejected-ideas.md, failed-approaches.md, CHANGELOG.md, "
        "sessions/ (dated per-person digests, each naming its author) and files/ (documents "
        "members' agents wrote in this project, such as drafts, decks and sheets, copied whole).",
        "When the user's request concerns this project's plans, features, TODOs, decisions or "
        "earlier work, read or grep that folder by its absolute path (it is hidden, so search "
        "tools skip it unless given the path). The live documents are current; sessions/ is "
        "history and may be superseded; check CHANGELOG.md for what changed recently.",
        "Never edit files there: the folder is overwritten from the server on every sync.",
    ]
    last = _state_map("synced").get(path)
    if have and error:
        lines.append(f"It may be out of date: the last update failed ({reason}); last successful "
                     f"sync {_when(last) if last else 'unknown'}.")
    elif last:
        lines.append(f"Last synced: {_when(last)}.")
    lines.append("</becos-project-repo>")
    return "\n".join(lines)


def under_root(path: object) -> bool:
    if not isinstance(path, str) or not path:
        return False
    full = os.path.realpath(os.path.expanduser(path))
    base = os.path.realpath(root())
    return full == base or full.startswith(base + os.sep)


def pre_tool_use(event: dict) -> dict | None:
    """Claude Code PreToolUse: allow the read-only tools inside the mirror root without a prompt
    (a hidden folder outside the workspace otherwise asks on every read). Anything else: no
    opinion, so the normal permission rules apply."""
    if not enabled() or event.get("tool_name") not in _READ_TOOLS:
        return None
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    target = tool_input.get("file_path") or tool_input.get("path")
    if not under_root(target):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "allow",
        "permissionDecisionReason": "Becos project folder (read-only mirror)"}}


# --- session end ----------------------------------------------------------------------------------

def session_end(cwd: str | None, session_id: object) -> bool:
    """Tell the server this user's session in the project ended (a compile hint; best effort)."""
    name = nocode_name(cwd, session_id)
    if not enabled() or name is None:
        return False
    project.call({"action": "session_end", "project_key": f"{project.NOCODE_PREFIX}{name}"})
    return True


def _spawn(command: str, cwd: str, session_id: object) -> None:
    args = [sys.executable, os.path.abspath(__file__), command, "--cwd", cwd]
    if isinstance(session_id, str) and session_id:
        args += ["--session", session_id]
    try:
        subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError:
        pass


def _hook_event() -> dict:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}


def _log_result(result: dict) -> None:
    status, path = result.get("status"), result.get("path")
    if status == "synced":
        capture._debug(f"project-repo: synced {path} {str(result.get('head'))[:12]} "
                       f"+{result['added']} ~{result['changed']} -{result['deleted']} "
                       f"({result['bytes']} bytes)")
    elif status == "unchanged":
        capture._debug(f"project-repo: unchanged {path}")
    else:
        capture._debug(f"project-repo: {status} {path}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="project_repo.py")
    parser.add_argument("command", choices=("sync", "status", "context", "session-end",
                                            "prompt-submit", "pre-tool-use", "session-end-call"))
    parser.add_argument("--cwd")
    parser.add_argument("--session")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "pre-tool-use":
            out = pre_tool_use(_hook_event())
            if out:
                print(json.dumps(out))
            return 0
        if args.command in ("prompt-submit", "session-end"):
            if not enabled():
                return 0
            event = _hook_event()
            cwd = event.get("cwd") or os.getcwd()
            session_id = event.get("session_id")
            if args.command == "prompt-submit":
                if due(cwd, session_id):
                    spawn_sync(cwd, session_id)
            elif nocode_name(cwd, session_id) is not None:
                _spawn("session-end-call", cwd, session_id)
            return 0
        cwd = args.cwd or os.getcwd()
        if args.command == "session-end-call":
            session_end(cwd, args.session)
            return 0
        if args.command == "context":
            text = context(cwd, args.session)
            if text and due(cwd, args.session):
                spawn_sync(cwd, args.session)
            print(json.dumps({"text": text}))
            return 0
        name = nocode_name(cwd, args.session)
        if args.command == "status":
            tenant = cached_tenant()
            print(json.dumps({"enabled": enabled(), "project": name,
                              "path": local_path(tenant, name) if tenant and name else None,
                              "root": root()}))
            return 0
        if not enabled() or name is None:
            return 0
        _log_result(sync(name, force=args.force))
        tenant = cached_tenant()
        if tenant:
            prune(tenant, keep=(name,))
        return 0
    except SyncError as exc:
        capture._debug(f"project-repo: failed {getattr(exc, 'path', '')}: {exc.category}"
                       f"{f' {exc.status}' if exc.status else ''}")
        return 0
    except Exception as exc:  # noqa: BLE001 — every entry point fails open
        capture._debug(f"project-repo: {args.command} failed: {type(exc).__name__}: "
                       f"{_redact(str(exc))}")
        return 0 if args.command != "status" else 1


if __name__ == "__main__":
    raise SystemExit(main())
