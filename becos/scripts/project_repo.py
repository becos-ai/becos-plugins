#!/usr/bin/env python3
"""Becos project repositories: a read-only local mirror of each nocode project's compiled folder.

    python3 project_repo.py sync [--cwd DIR] [--session ID] [--force]   clone or update (detached)
    python3 project_repo.py status [--cwd DIR] [--session ID]           path + last sync, as JSON
    python3 project_repo.py context [--cwd DIR] [--session ID]          {"text"}: the session block,
                                                                        and a detached sync if due
    python3 project_repo.py session-end                                 SessionEnd hook (stdin JSON)
    python3 project_repo.py prompt-submit                               per-prompt hook (stdin JSON)
    python3 project_repo.py pre-tool-use                                Claude Code PreToolUse hook

vonic-stack ``docs/PLAN_becos_project_repo.md``. The Becos server compiles every member's work in a
nocode project into a git repository (digests in ``sessions/``, live documents: strategy, roadmap,
TODOs, features, decisions, rejected ideas, failed approaches). This module keeps a copy at
``~/.becos/projects/<tenant>/<project>/`` (``VONIC_CODECOLLAB_PROJECTS_DIR`` moves it) so the agent
can grep and read it like any files:

* Only the server writes (D1). The mirror is fetched and hard-reset to ``origin/main``, then
  cleaned (P9): local edits are overwritten by design, and a server-side history rewrite (a purge)
  converges on the next sync.
* Clone when a session starts in a nocode project (or one is chosen or created), then fetch at
  most every ``VONIC_CODECOLLAB_PROJECT_REPO_INTERVAL`` seconds (default 300) while the session is
  active. Always in a detached child: a hook never waits on the network.
* git authenticates with the same short-lived becos token the MCP calls use, handed to git by
  a one-line credential helper through this process's environment, never written to
  ``.git/config`` or put on a command line.
* A non-blocking ``flock`` per repo: a second session on the machine skips that round.
* ``git`` missing or anything failing: logged in debug, never raised into a hook.
* A mirror whose project is gone from the team's project list is deleted (:func:`prune`): checked
  at most every 6 hours from the background sync, and at once when the server reports the
  project's repository missing. Only folders this module cloned are touched.

Off unless ``VONIC_CODECOLLAB_PROJECT_REPO=1`` (rollout: on in dev profiles first).
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — becos config, token, debug log
import connect  # noqa: E402 — atomic writes
import project  # noqa: E402 — the session's project

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DEFAULT_INTERVAL = 300
_GIT_TIMEOUT = 120
LIVE_DOCS = ("product-strategy.md", "roadmap.md", "todos.md", "features/", "decisions/",
             "rejected-ideas.md", "failed-approaches.md", "files/")
# The Claude Code tools that only read; the PreToolUse hook allows these inside the mirror root.
_READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS"})
_PRUNE_EVERY = 6 * 3600
# The server lists at most this many projects; a listing this long may be cut off, so no prune.
_LIST_CEILING = 500


def enabled() -> bool:
    return (os.environ.get("VONIC_CODECOLLAB_PROJECT_REPO", "0").strip() == "1"
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
    tenant = (_state().get("tenants") or {}).get(_gateway_host(), "")
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


# --- git ----------------------------------------------------------------------------------------

# The helper only ever answers for the remote git is talking to (git asks it per URL); the token
# reaches it through the child's environment, which only this user can read.
_HELPER = '!f() { test "$1" = get && echo username=becos && echo "password=$BECOS_GIT_TOKEN"; }; f'


def _git(args: list[str], *, cwd: str | None, token: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": "",
           "BECOS_GIT_TOKEN": token, "LC_ALL": "C"}
    base = ["git", "-c", "credential.helper=", "-c", f"credential.helper={_HELPER}",
            "-c", "core.hooksPath=/dev/null"]
    return subprocess.run(base + args, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=_GIT_TIMEOUT, check=False)


class SyncError(RuntimeError):
    pass


def _lock(path: str):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fh = open(path, "a")  # noqa: SIM115 — held for the sync, closed by the caller
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def sync(name: str, *, force: bool = False) -> dict:
    """Clone or update one project's mirror. Returns a status dict; raises SyncError on a failure
    worth logging. Skips (``skipped``) when another process holds the lock or the last sync was
    under the interval ago (unless ``force``)."""
    if shutil.which("git") is None:
        raise SyncError("git is not installed")
    url, token = capture._resolve_becos()
    tenant = token_tenant(token)
    if not tenant:
        raise SyncError("the becos token names no team")
    _save_state({"tenants": {**(_state().get("tenants") or {}), _gateway_host(): tenant}})
    path = local_path(tenant, name)
    missing = not os.path.isdir(os.path.join(path, ".git"))
    stamps = _state().get("synced") or {}
    last = stamps.get(path, 0) if isinstance(stamps, dict) else 0
    if not force and not missing and time.time() - float(last or 0) < _interval():
        return {"status": "skipped", "reason": "recent", "path": path}
    lock = _lock(os.path.join(root(), ".locks", f"{tenant}-{name}.lock"))
    if lock is None:
        return {"status": "skipped", "reason": "locked", "path": path}
    try:
        remote = remote_url(tenant, name)
        if not os.path.isdir(os.path.join(path, ".git")):
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            if os.path.exists(path):
                shutil.rmtree(path)      # a half-written earlier clone; the server is the source
            done = _git(["clone", "--quiet", "--origin", "origin", "--branch", "main", remote,
                         path], cwd=None, token=token)
            action = "cloned"
        else:
            done = _git(["fetch", "--quiet", "--prune", "origin",
                         "+refs/heads/main:refs/remotes/origin/main"], cwd=path, token=token)
            if done.returncode == 0:
                done = _git(["reset", "--quiet", "--hard", "origin/main"], cwd=path)
            if done.returncode == 0:
                done = _git(["clean", "-qfdx"], cwd=path)
            action = "updated"
        if done.returncode != 0:
            if action == "updated" and _repo_missing(done.stderr):
                # The project may have been deleted: check the listing now, not in 6 hours.
                lock.close()             # prune takes this repo's lock itself
                removed = prune(tenant, keep=(), force=True)
                if path in removed:
                    return {"status": "removed", "path": path}
            raise SyncError(_reason(done.stderr))
        head = _git(["rev-parse", "--short", "HEAD"], cwd=path).stdout.strip()
        _save_state({"synced": {**(_state().get("synced") or {}), path: time.time()}})
        return {"status": action, "path": path, "head": head}
    finally:
        lock.close()


def _repo_missing(stderr: str) -> bool:
    text = (stderr or "").lower()
    # HTTP: "repository '…' not found" (the git server's 404); a local remote (tests): the path
    # is no longer a repository.
    return ("not found" in text or "error: 404" in text
            or "does not appear to be a git repository" in text)


def _origin_matches(path: str, tenant: str, name: str) -> bool:
    """``path`` is a mirror this module cloned: its origin is this project's /git/ URL."""
    done = _git(["config", "--get", "remote.origin.url"], cwd=path)
    return done.returncode == 0 and done.stdout.strip().endswith(f"/git/{tenant}/{name}.git")


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
    pruned_at = (_state().get("pruned") or {}).get(tenant, 0)
    if not force and time.time() - float(pruned_at or 0) < _PRUNE_EVERY:
        return []
    listed = listed_names() if names is None else names
    if listed is None:
        return []
    _save_state({"pruned": {**(_state().get("pruned") or {}), tenant: time.time()}})
    removed = []
    for entry in sorted(os.listdir(base)):
        path = os.path.join(base, entry)
        if (entry in listed or entry in keep or not project.valid_name(entry)
                or os.path.islink(path) or not os.path.isdir(os.path.join(path, ".git"))
                or not _origin_matches(path, tenant, entry)):
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
        stamps = _state().get("synced") or {}
        _save_state({"synced": {k: v for k, v in stamps.items() if k not in removed}})
    return removed


def _reason(stderr: str) -> str:
    text = (stderr or "").strip().splitlines()
    last = text[-1] if text else "git failed"
    # Never echo anything token-shaped back into a log.
    return re.sub(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", "<token>", last)[:300]


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
        if not os.path.isdir(os.path.join(path, ".git")):
            return True
    except ValueError:
        return True
    stamps = _state().get("synced") or {}
    last = stamps.get(local_path(tenant, name), 0) if isinstance(stamps, dict) else 0
    return time.time() - float(last or 0) >= _interval()


# --- context for the agent ----------------------------------------------------------------------

def context(cwd: str | None, session_id: object) -> str:
    """The session-start block naming the mirror, its documents and the precedence rule; "" when
    off or the session isn't in a nocode project. Local only."""
    if not enabled():
        return ""
    name = nocode_name(cwd, session_id)
    if name is None:
        return ""
    tenant = cached_tenant()
    path = local_path(tenant, name) if tenant else os.path.join(root(), "<team>", name)
    present = os.path.isdir(os.path.join(path, ".git"))
    stamps = _state().get("synced") or {}
    last = stamps.get(path) if isinstance(stamps, dict) else None
    lines = [
        "<becos-project-repo>",
        f"This session's Becos project `{name}` has a compiled, read-only folder on this machine: "
        f"`{path}`" + ("" if present else " (being downloaded now; it appears within a minute)")
        + ".",
        "It holds what the team worked out in this project, written by the Becos server from "
        "every member's sessions: README.md (index), product-strategy.md, roadmap.md, todos.md, "
        "features/<feature>.md, decisions/, rejected-ideas.md, failed-approaches.md, CHANGELOG.md, "
        "sessions/ (dated per-person digests) and files/ (documents members' agents wrote in "
        "this project, such as drafts, decks and sheets, copied whole).",
        "When the user's request concerns this project's plans, features, TODOs, decisions or "
        "earlier work, read or grep that folder by its absolute path (it is hidden, so search "
        "tools skip it unless given the path). The live documents are current; sessions/ is "
        "history and may be superseded; check CHANGELOG.md for what changed recently.",
        "Never edit files there: the folder is overwritten from the server on every sync.",
    ]
    if last:
        lines.append(f"Last synced: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}.")
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
        result = sync(name, force=args.force)
        capture._debug(f"project-repo: {result['status']} {result['path']}")
        tenant = cached_tenant()
        if tenant:
            prune(tenant, keep=(name,))
        return 0
    except Exception as exc:  # noqa: BLE001 — every entry point fails open
        capture._debug(f"project-repo: {args.command} failed: {type(exc).__name__}: {exc}")
        return 0 if args.command != "status" else 1


if __name__ == "__main__":
    raise SystemExit(main())
