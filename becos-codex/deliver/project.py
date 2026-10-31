#!/usr/bin/env python3
"""Projects: which project a coding session's memory is filed under (vonic_projects).

    python3 project.py [--cwd DIR] [--session ID] status [--json]   this session's project
    python3 project.py [--cwd DIR] --session ID start               session start: resolve, pin,
                                                                    print {resolution, message}
    python3 project.py [--cwd DIR] [--session ID] list [--json]     projects to choose from
    python3 project.py [--cwd DIR] --session ID use NAME            use an existing project
    python3 project.py [--cwd DIR] --session ID use --new [NAME]    create one (NAME optional)
    python3 project.py refresh                                      update the local list cache

A project is a git repo (``git-repo``, key ``org/repo``) or a project without one
(``nocode-project``, key ``nocode:<name>``). The key is sent as ``repo_name``, so recall, capture,
rulebook and handoff treat a project exactly like a repo. Resolution, first match wins:

1. The session folder (or a parent) is a git repo with an ``origin`` remote: that repo. Always; it
   cannot be changed. A git repo without a remote counts as a plain folder.
2. This session already has a project (chosen, switched, or pinned at session start).
3. ``.becos-project`` in the folder or a parent, stopping below ``~`` (home itself is never
   checked): the current user's ``default``.
4. Otherwise the user is asked; ``use`` records the answer. With no answer, ``auto`` creates a
   project with a server-generated ``<adjective>-<scientist>`` name.

``.becos-project`` is per user (``{"version": 1, "users": {<login>: {"default", "projects"}}}``);
other users' entries are never read for, shown to or changed by this user. A choice made where the
user has no entry writes one (``default`` = the choice); a later switch is session-only and only
adds the project to the user's ``projects`` list. Invalid JSON is never overwritten.

On by default: ``VONIC_CODECOLLAB_PROJECTS=0`` opts out. Exit codes: 0 ok, 1 the server call failed, 2 usage
error, git repo (cannot change) or no project yet. Everything that runs from a hook fails open.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — becos config, git helpers, identity
import connect  # noqa: E402 — atomic writes (private cache files)
import gbrain_client  # noqa: E402

TOOL = "vonic_projects"
DOT_FILE = ".becos-project"
DIRECTORIES_FILE = "directories.json"
TYPE_GIT = "git-repo"
TYPE_NOCODE = "nocode-project"
TYPE_NONE = "coding-mode"
NOCODE_PREFIX = "nocode:"
NOCODE_BRANCH = "none"        # branch_name needs 1+ chars; a nocode project has no branches
_DEFAULT_TIMEOUT = 20.0
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_SESSION_MAX_AGE_S = 30 * 86_400
_MAX_LISTED = 15


def enabled(env: dict | None = None) -> bool:
    """Default on; an explicit value must be ``1`` (trimmed) to enable it."""
    source = os.environ if env is None else env
    return source.get("VONIC_CODECOLLAB_PROJECTS", "1").strip() == "1"


def becos_dir() -> str:
    """The root directory for Becos configuration and state (~/.becos)."""
    configured = os.environ.get("VONIC_BECOS_DIR", "").strip()
    return os.path.abspath(os.path.expanduser(configured or "~/.becos"))


def directories_path() -> str:
    """The centralized directory-to-project mappings file (~/.becos/directories.json)."""
    return os.path.join(becos_dir(), DIRECTORIES_FILE)


def read_directories() -> dict:
    """Read ~/.becos/directories.json, returning {"version": 1, "directories": {...}, "recent_projects": [...]}."""
    path = directories_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            data.setdefault("version", 1)
            data.setdefault("directories", {})
            data.setdefault("recent_projects", [])
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "directories": {}, "recent_projects": []}


def write_directory_entry(cwd: str, project_name: str | None, project_type: str = TYPE_NOCODE) -> dict:
    """Record a directory mapping in ~/.becos/directories.json."""
    real = os.path.realpath(cwd)
    data = read_directories()
    dirs = data.setdefault("directories", {})
    dirs[real] = {
        "project": project_name,
        "type": project_type,
        "updated_at": time.time(),
    }
    recents = [p for p in data.get("recent_projects") or [] if p != project_name and p]
    if project_name:
        recents.insert(0, project_name)
    data["recent_projects"] = recents[:15]
    os.makedirs(becos_dir(), mode=0o700, exist_ok=True)
    connect._atomic_write(directories_path(), data)
    return data


def find_directory_entry(cwd: str | None, *, home: str | None = None) -> tuple[str, dict] | tuple[None, None]:
    """The nearest directory mapping from cwd upward, stopping below home (never home itself).
    Returns (matched_dir_path, entry_dict) or (None, None)."""
    home = os.path.realpath(home or os.path.expanduser("~"))
    path = os.path.realpath(cwd or os.getcwd())
    data = read_directories()
    dirs = data.get("directories") or {}
    curr = path
    while True:
        if curr == home or curr == os.path.dirname(curr):
            break
        if curr in dirs and isinstance(dirs[curr], dict):
            return curr, dirs[curr]
        curr = os.path.dirname(curr)
    # Legacy migration fallback
    return _check_legacy_dot(cwd, home=home)


def _check_legacy_dot(cwd: str | None, *, home: str | None = None) -> tuple[str, dict] | tuple[None, None]:
    """Check for legacy .becos-project, and if found, import into directories.json once."""
    dot = find_dot(cwd, home=home)
    if not dot:
        return None, None
    try:
        entry = user_entry(read_dot(dot))
        if entry and entry.get("default"):
            name = entry["default"]
            type_ = TYPE_GIT if "/" in name else TYPE_NOCODE
            folder = os.path.dirname(dot)
            write_directory_entry(folder, name, type_)
            return folder, {"project": name, "type": type_, "updated_at": time.time(), "legacy": True}
    except Exception as exc:
        capture._debug(f"project: ignoring legacy {dot}: {exc}")
    return None, None


def _timeout() -> float:
    try:
        value = float(os.environ.get("VONIC_PROJECTS_TIMEOUT", "") or _DEFAULT_TIMEOUT)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def user_key() -> str:
    """Whose entry in ``.becos-project``: the becos login name, else the OS user."""
    return capture._cached_author() or os.environ.get("USER") or "default"


def _client_tag() -> str:
    tag = (os.environ.get("VONIC_CODECOLLAB_CLIENT_TAG") or "cc").strip()
    return tag if re.fullmatch(r"[a-z0-9_-]{1,16}", tag) else "cc"


def command_name() -> str:
    """The project command as this client spells it."""
    return "/becos:project" if _client_tag() == "cc" else "/becos-project"


# --- git ------------------------------------------------------------------------------------------


def git_project(cwd: str | None) -> str | None:
    """The ``org/repo`` of the git repo containing ``cwd`` when it has an ``origin`` remote."""
    cwd = cwd or os.getcwd()
    if not capture._is_git(cwd):
        return None
    slug = capture._origin_slug(cwd)
    return slug if slug and slug.count("/") == 1 else None


# --- .becos-project -------------------------------------------------------------------------------


def find_dot(cwd: str | None, *, home: str | None = None) -> str | None:
    """The nearest ``.becos-project`` from ``cwd`` upward, stopping below ``home`` (never home
    itself, never the filesystem root)."""
    home = os.path.realpath(home or os.path.expanduser("~"))
    path = os.path.realpath(cwd or os.getcwd())
    while True:
        if path == home or path == os.path.dirname(path):
            return None
        candidate = os.path.join(path, DOT_FILE)
        if os.path.isfile(candidate) and not os.path.islink(candidate):
            return candidate
        path = os.path.dirname(path)


class DotFileInvalid(ValueError):
    pass


def read_dot(path: str) -> dict:
    """The parsed file as ``{"version", "users": {...}, ...}``. A plain one-line file, or JSON
    without ``users``, is a legacy file: its project becomes the current user's ``default``."""
    with open(path, encoding="utf-8") as fh:
        raw = fh.read(64_000)
    text = raw.strip()
    if not text:
        return {"version": 1, "users": {}}
    if not text.startswith("{"):
        name = text.splitlines()[0].strip()
        return {"version": 1, "users": {user_key(): {"default": name, "projects": [name]}}}
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DotFileInvalid(f"{path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise DotFileInvalid(f"{path} is not a JSON object")
    if not isinstance(data.get("users"), dict):
        legacy = data.get("default")
        users = ({user_key(): {"default": legacy, "projects": [legacy]}}
                 if isinstance(legacy, str) and legacy.strip() else {})
        data = {**{k: v for k, v in data.items() if k not in ("default", "projects")},
                "users": users}
    data.setdefault("version", 1)
    return data


def user_entry(data: dict, user: str | None = None) -> dict | None:
    """This user's ``{"default", "projects"}``, else None (other users' entries never count)."""
    entry = (data.get("users") or {}).get(user or user_key())
    if not isinstance(entry, dict):
        return None
    default = entry.get("default")
    if not isinstance(default, str) or not default.strip():
        return None
    projects = [p for p in entry.get("projects") or [] if isinstance(p, str) and p.strip()]
    if default not in projects:
        projects.insert(0, default)
    return {**entry, "default": default.strip(), "projects": projects}


def write_dot(path: str, *, default: str | None = None, add: tuple[str, ...] = ()) -> dict:
    """Update this user's entry in ``path`` (created when absent): set ``default`` (only when
    given) and append ``add`` to ``projects``. Re-reads first and keeps everything else; an invalid
    existing file raises :class:`DotFileInvalid` and is left untouched."""
    data = read_dot(path) if os.path.exists(path) else {"version": 1, "users": {}}
    users = data.setdefault("users", {})
    user = user_key()
    entry = users.get(user) if isinstance(users.get(user), dict) else {}
    projects = [p for p in entry.get("projects") or [] if isinstance(p, str) and p.strip()]
    if default:
        entry["default"] = default
    for name in (default, *add):
        if name and name not in projects:
            projects.append(name)
    entry["projects"] = projects
    users[user] = entry
    fd, tmp = tempfile.mkstemp(prefix=f".{DOT_FILE}.", dir=os.path.dirname(path) or ".")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return data


# --- local state ----------------------------------------------------------------------------------


def _state_dir(*parts: str) -> str:
    path = os.path.join(capture._cache_dir(), "project", *parts)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _session_path(session_id: object) -> str | None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return os.path.join(_state_dir(_client_tag()), f"{session_id}.json")


def read_session(session_id: object) -> dict | None:
    path = _session_path(session_id)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("key") else None


def write_session(session_id: object, resolution: Resolution) -> None:
    path = _session_path(session_id)
    if path:
        connect._atomic_write(path, {**dataclasses.asdict(resolution), "at": time.time()})
        _sweep_sessions()


def _sweep_sessions() -> None:
    cutoff = time.time() - _SESSION_MAX_AGE_S
    try:
        for entry in os.scandir(_state_dir(_client_tag())):
            if entry.name.endswith(".json") and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
    except OSError:
        pass


def _list_cache_path() -> str:
    try:
        url = capture._resolve_becos_url()
    except Exception:  # noqa: BLE001 — no config: still a stable key
        url = ""
    key = hashlib.sha256(f"{url}\x1f{capture._cached_author()}".encode()).hexdigest()[:32]
    return os.path.join(_state_dir(), f"list-{key}.json")


def cached_list() -> dict:
    try:
        with open(_list_cache_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def key_for(name: str, listing: dict | None = None) -> tuple[str, str]:
    """``(key, type)`` for a project name: a name with ``/`` is a git repo; otherwise the listed
    project of that name, else ``nocode:<name>`` (what the server creates for a new name)."""
    if "/" in name:
        return name, TYPE_GIT
    for p in (listing if listing is not None else cached_list()).get("projects") or []:
        if isinstance(p, dict) and p.get("name") == name and p.get("key"):
            return str(p["key"]), str(p.get("type") or TYPE_NOCODE)
    return f"{NOCODE_PREFIX}{name}", TYPE_NOCODE


# --- resolution -----------------------------------------------------------------------------------


@dataclasses.dataclass
class Resolution:
    key: str
    name: str
    type: str
    source: str                 # git | directory_map | dotfile | chosen | switched | auto
    dot_path: str = ""
    others: list[str] = dataclasses.field(default_factory=list)


def resolve(cwd: str | None, session_id: object = None) -> Resolution | None:
    """This session's project from local state only (git, session choice, directories.json, or legacy
    .becos-project), or None when the user has to be asked. Never calls the server."""
    cwd = cwd or os.getcwd()
    slug = git_project(cwd)
    if slug:
        return Resolution(slug, slug, TYPE_GIT, "git")
    pinned = read_session(session_id)
    if pinned:
        fields = {f.name for f in dataclasses.fields(Resolution)}
        return Resolution(**{k: v for k, v in pinned.items() if k in fields})
    matched_path, entry = find_directory_entry(cwd)
    if not matched_path or not entry:
        return None
    proj_type = entry.get("type") or TYPE_NOCODE
    proj_name = entry.get("project")
    if proj_type == TYPE_NONE or not proj_name:
        return Resolution("", "none", TYPE_NONE, "directory_map", matched_path, [])
    key, type_ = key_for(proj_name)
    data = read_directories()
    recents = [p for p in data.get("recent_projects") or [] if p != proj_name]
    source = "dotfile" if entry.get("legacy") else "directory_map"
    return Resolution(key, proj_name, type_, source, matched_path, recents[:5])


def is_coding_mode(cwd: str | None, session_id: object = None) -> bool:
    """Whether this session or folder is resolved to coding-mode (no memory capture)."""
    resolved = resolve(cwd, session_id)
    return resolved is not None and resolved.type == TYPE_NONE


def file_persistence(cwd: str | None, session_id: object = None, *,
                     pipeline_enabled: bool = True) -> dict:
    """Declare enabled local hooks, not delivery success. No network or user-wide default.

    Adapters with optional capture must pass its enabled state or set the host-owned
    VONIC_CODECOLLAB_PERSIST_PIPELINE=0. Opt-outs never imply conversational fallback.
    """
    import shared_handoff  # noqa: PLC0415 — lazy: it also resolves projects through capture
    resolved = resolve(cwd, session_id) if enabled() else None
    key = resolved.key if resolved and resolved.type == TYPE_NOCODE else None
    on = bool(key and pipeline_enabled and shared_handoff.persist_enabled())
    return {"project_key": key, "client_session_id": str(session_id or ""),
            "upload_owner": "client_hooks" if on else "none", "autosave_enabled": on,
            "client_hooks_enabled": on, "receipt_requirement": "sealed"}


def persistence_instructions(context: dict) -> str:
    """One shared owner declaration and duplicate-upload guard for every adapter."""
    state = json.dumps(context, ensure_ascii=False, sort_keys=True)
    if context.get("upload_owner") == "client_hooks":
        rule = ("The enabled client persistence pipeline owns created/modified workspace files. "
                "Do NOT also call vonic_save_project_file or vonic_project_files put/prepare/complete "
                "for automatic persistence. If vonic_project_session is used for this conversation, "
                "declare upload_owner=client_hooks and client_hooks_enabled=true; retain its returned "
                "logical session_id/revision separately from client_session_id. Never silently fall "
                "back: explicitly transfer ownership only after queued hook work is drained/cancelled. "
                "Enabled is not a successful upload: without a matching sealed receipt report "
                "pending/unverified, not saved. Sealed proves storage, not a Git commit.")
    else:
        rule = ("Automatic persistence is disabled or out of scope for this client session. "
                "Do not enable conversational autosave as a fallback or bypass an opt-out/Coding Mode. "
                "Explicit user-requested one-off saves remain separate from autosave.")
    return "## becos file persistence\n" + state + "\n" + rule


def context_resolution(resolved: Resolution | None, cwd: str, session_id: object) -> dict | None:
    if resolved is None:
        return None
    persistence = file_persistence(cwd, session_id)
    return {**dataclasses.asdict(resolved), "file_persistence": persistence,
            "file_persistence_instructions": persistence_instructions(persistence)}


def identity(cwd: str, session_id: object = None) -> dict:
    """The capture identity ``{repo, branch, author, commit}`` for a turn in ``cwd``, or ``{}``
    when the session has no project (yet) or is in coding mode: a git repo with a remote as before,
    else the session's project (branch ``none`` and no commit outside git)."""
    resolved = resolve(cwd, session_id)
    if resolved is None or resolved.type == TYPE_NONE:
        return {}
    in_git = capture._is_git(cwd)
    return {
        "repo": resolved.key,
        "branch": (capture._branch_or_none(cwd) or "detached") if in_git else NOCODE_BRANCH,
        "author": capture._author_name(cwd),
        "commit": capture._full_commit(cwd) if in_git else None,
    }


def start(cwd: str | None, session_id: object = None) -> Resolution | None:
    """Session start: resolve locally and pin a directory_map or dotfile result to the session, so an
    edit to the file mid-session never moves the session's later turns."""
    resolved = resolve(cwd, session_id)
    if resolved is not None and resolved.source in ("directory_map", "dotfile"):
        write_session(session_id, resolved)
    if resolved is not None and resolved.type == TYPE_NOCODE:
        try:
            import project_repo  # noqa: PLC0415
            if project_repo.enabled():
                tenant = project_repo.cached_tenant()
                missing = True
                if tenant:
                    try:
                        p = project_repo.local_path(tenant, resolved.name)
                        missing = not project_repo.present(p)
                    except ValueError:
                        missing = True
                if missing:
                    try:
                        project_repo.sync(resolved.name, force=True)
                    except Exception:
                        project_repo.spawn_sync(cwd, session_id, force=True)
        except Exception:
            pass
    return resolved


def message(resolved: Resolution | None) -> str:
    """The one-line notice shown to the user at session start."""
    if resolved is None:
        return ("becos: this folder has no project yet; you'll be asked which project this "
                "conversation is for.")
    if resolved.source == "git":
        return (f"becos: this conversation is saved to project {resolved.name} (git repo).")
    if resolved.type == TYPE_NONE:
        return ("becos: coding mode (no project memory attached). "
                f"To save this session to a project, run {command_name()}.")
    where = f" (from {_short(resolved.dot_path)})" if resolved.dot_path else ""
    also = f" Also used here: {', '.join(resolved.others[:5])}." if resolved.others else ""
    return (f"becos: this conversation is saved to project {resolved.name}{where}.{also} "
            f"To use a different project for this session, ask the agent or run "
            f"{command_name()}.")


def _short(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + os.sep) else path


# --- server ---------------------------------------------------------------------------------------


class ProjectError(RuntimeError):
    def __init__(self, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.data = data or {}


def _decode(result: dict) -> dict:
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and ("result" not in structured or len(structured) > 1):
        return structured
    if isinstance(structured, dict) and isinstance(structured.get("result"), dict):
        return structured["result"]
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                data = json.loads(item.get("text") or "")
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    raise ProjectError("unexpected response from the server")


def call(arguments: dict) -> dict:
    url, token = capture._resolve_becos()
    result = gbrain_client.call_tool(url, token, TOOL, arguments, _timeout(),
                                     extra_headers=capture._becos_identity_headers())
    if result.get("isError"):
        raise ProjectError("the server returned an error")
    data = _decode(result)
    if data.get("error"):
        raise ProjectError(str(data["error"]), data)
    return data


def refresh() -> dict:
    data = call({"action": "list"})
    connect._atomic_write(_list_cache_path(), {**data, "fetched_at": time.time()})
    return data


def create(name: str | None = None, *, exist_ok: bool = False) -> dict:
    args: dict = {"action": "create", "exist_ok": exist_ok}
    if name:
        args["name"] = name
    project = call(args).get("project") or {}
    if not project.get("key"):
        raise ProjectError("the server returned no project")
    return project


# --- operations -----------------------------------------------------------------------------------


def options(cwd: str | None, session_id: object = None) -> dict:
    """What to offer: this user's projects from directories.json first, then the tenant's other
    projects (most recent first), and a free suggested name for a new one."""
    cwd = cwd or os.getcwd()
    try:
        listing = refresh()
    except (ProjectError, gbrain_client.GbrainError):
        listing = cached_list()
        if not listing:
            raise
    matched_path, entry = find_directory_entry(cwd)
    data = read_directories()
    mine: list[str] = []
    if entry and entry.get("project"):
        mine.append(entry["project"])
    for p in data.get("recent_projects") or []:
        if p not in mine:
            mine.append(p)
    dot = find_dot(cwd)
    if dot:
        try:
            d_entry = user_entry(read_dot(dot))
            if d_entry:
                for p in d_entry.get("projects") or []:
                    if p not in mine:
                        mine.append(p)
        except (OSError, DotFileInvalid):
            pass
    projects = [p for p in listing.get("projects") or [] if isinstance(p, dict) and p.get("name")]
    by_name = {p["name"]: p for p in projects}
    ordered = [by_name.get(n) or {"name": n, "key": key_for(n, listing)[0],
                                  "type": key_for(n, listing)[1]} for n in mine]
    ordered += [p for p in projects if p["name"] not in mine]
    current = resolve(cwd, session_id)
    return {"current": dataclasses.asdict(current) if current else None,
            "git_repo": git_project(cwd),
            "projects": [{"name": p["name"], "key": p.get("key"), "type": p.get("type"),
                          "in_file": p["name"] in mine} for p in ordered],
            "suggested_name": listing.get("suggested_name") or "",
            "dot_path": matched_path or dot or ""}


def use(cwd: str | None, session_id: object, name: str | None = None, *, new: bool = False,
        none: bool = False, remember: bool | None = None, source: str | None = None) -> Resolution:
    """Record this session's project: an existing ``name``, or a new one (``new``; ``name``
    optional = server-generated), or coding mode (``none``). Where this user has no entry,
    the choice is written as default; otherwise it is session-only and added to the user's list."""
    cwd = cwd or os.getcwd()
    slug = git_project(cwd)
    if slug:
        raise ValueError(f"this session is in git repo {slug}; its project can't be changed")
    if not _session_path(session_id):
        raise ValueError("a session id is required")

    if none or name == "--none":
        resolved = Resolution("", "none", TYPE_NONE, source or "chosen", cwd)
        write_session(session_id, resolved)
        if remember is not False:
            try:
                write_directory_entry(cwd, None, TYPE_NONE)
            except OSError as exc:
                capture._debug(f"project: could not write directory entry: {exc}")
        return resolved

    name = (name or "").strip() or None
    if new:
        if name is not None and not valid_name(name):
            raise ValueError("a new project name must be lowercase letters, digits and dashes")
        project = create(name)
        key, name, type_ = project["key"], project["name"], project.get("type") or TYPE_NOCODE
    else:
        if not name:
            raise ValueError("give a project name, or --new")
        listing = cached_list()
        if not any(p.get("name") == name for p in listing.get("projects") or []):
            try:
                listing = refresh()
            except (ProjectError, gbrain_client.GbrainError):
                pass
        if "/" in name or any(p.get("name") == name for p in listing.get("projects") or []):
            key, type_ = key_for(name, listing)
        elif valid_name(name):
            project = create(name, exist_ok=True)   # e.g. a name typed into .becos-project
            key, name, type_ = project["key"], project["name"], project.get("type") or TYPE_NOCODE
        else:
            raise ValueError(f"no project named {name!r}; use --new to create one")

    matched_path, entry = find_directory_entry(cwd)
    should_remember = remember if remember is not None else (entry is None)
    target = matched_path or cwd
    if entry:
        source = source or ("chosen" if name == entry.get("project") else "switched")
    else:
        source = source or "chosen"

    dot = find_dot(cwd)
    if dot:
        try:
            d_entry = user_entry(read_dot(dot))
            if d_entry:
                write_dot(dot, add=(name,))
            else:
                write_dot(dot, default=name)
        except (OSError, DotFileInvalid):
            pass

    if should_remember:
        try:
            write_directory_entry(cwd, name, type_)
        except OSError as exc:
            capture._debug(f"project: could not write directory entry: {exc}")
    else:
        try:
            data = read_directories()
            recents = [p for p in data.get("recent_projects") or [] if p != name and p]
            recents.insert(0, name)
            data["recent_projects"] = recents[:15]
            os.makedirs(becos_dir(), mode=0o700, exist_ok=True)
            connect._atomic_write(directories_path(), data)
        except OSError:
            pass

    resolved = Resolution(key, name, type_, source, target)
    write_session(session_id, resolved)
    try:
        import project_repo  # noqa: PLC0415 — lazy: it imports this module
        if project_repo.enabled() and resolved.type == TYPE_NOCODE:
            tenant = project_repo.cached_tenant()
            missing = True
            if tenant:
                try:
                    p = project_repo.local_path(tenant, resolved.name)
                    missing = not project_repo.present(p)
                except ValueError:
                    missing = True
            if missing:
                try:
                    project_repo.sync(resolved.name, force=True)
                except Exception as exc:
                    capture._debug(f"project: initial download failed: {exc}")
                    project_repo.spawn_sync(cwd, session_id, force=True)
            else:
                project_repo.spawn_sync(cwd, session_id, force=True)
        else:
            project_repo.spawn_sync(cwd, session_id, force=True)
    except Exception as exc:  # noqa: BLE001 — never fails the choice
        capture._debug(f"project: repo sync not started: {type(exc).__name__}")
    return resolved
    try:
        import project_repo  # noqa: PLC0415 — lazy: it imports this module
        project_repo.spawn_sync(cwd, session_id, force=True)   # download the chosen project's folder
    except Exception as exc:  # noqa: BLE001 — never fails the choice
        capture._debug(f"project: repo sync not started: {type(exc).__name__}")
    return resolved


def auto(cwd: str | None, session_id: object) -> Resolution:
    """No answer: a new project with a server-generated name, written like a first choice."""
    return use(cwd, session_id, None, new=True, source="auto")


def spawn_refresh(cwd: str | None) -> bool:
    """Refresh the project list cache in a detached child (session start must stay local)."""
    if not enabled():
        return False
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "refresh"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, cwd=cwd or None)
    except OSError:
        return False
    return True


def ask_instructions(cwd: str, session_id: object) -> str:
    """Session-start context telling the model to ask the user which project this is."""
    script = os.path.abspath(__file__)
    base = f'python3 "{script}" --cwd "{cwd}" --session "{session_id}"'
    if _client_tag() in ("cc", "cx"):
        # These runtimes bundle becos's tool server (codecollab_mcp.py); the script stays the
        # fallback for a session where it is not running.
        return "\n".join([
            "## becos project",
            "This folder has no becos project yet. Before working on the user's first request, "
            "ask which project this conversation is for, then record the answer:",
            "1. Call `becos_project` for the choices (their projects in this folder first, then "
            "the team's) and a suggested name for a new project.",
            "2. Ask the user with your question tool when you have one (a few likely projects plus "
            "\"New project: <suggested name>\"; free text accepts any other name), else as a short "
            "numbered list.",
            "3. Call `becos_project_use` with `name` for an existing project, or `new: true` (name "
            "optional) for a new one; the user approves recording it.",
            f"If those tools are unavailable, run `{base} list` and `{base} use <name>` / "
            f"`{base} use --new [<name>]` instead.",
            "Then continue with the request. If the user ignores the question, carry on; becos "
            "names a new project itself and tells them.",
        ])
    return "\n".join([
        "## becos project",
        "This folder has no becos project yet. Before working on the user's first request, "
        "ask which project this conversation is for, then record the answer:",
        f"1. Run `{base} list` for the choices (their projects in this folder first, then the "
        "team's) and a suggested name for a new project.",
        "2. Ask the user with your question tool when you have one (a few likely projects plus "
        "\"New project: <suggested name>\"; free text accepts any other name), else as a short "
        "numbered list.",
        f"3. Run `{base} use <name>` for an existing project, or `{base} use --new [<name>]` for a "
        "new one (no name = the suggested-style generated name).",
        "Then continue with the request. If the user ignores the question, carry on; becos "
        "names a new project itself and tells them.",
    ])


def format_options(data: dict) -> str:
    lines = []
    current = data.get("current")
    if data.get("git_repo"):
        lines.append(f"This session is in git repo {data['git_repo']}: its project can't be "
                     "changed.")
    elif current:
        if current.get("type") == TYPE_NONE:
            lines.append("Current mode: Coding Mode (no project memory attached).")
        else:
            lines.append(f"Current project: {current['name']} ({current['source']}).")
    projects = data.get("projects") or []
    if projects:
        lines.append("Projects (yours in this folder first):")
        for n, p in enumerate(projects[:_MAX_LISTED], 1):
            mark = " [in .becos-project]" if p.get("in_file") else ""
            lines.append(f"{n}. {p['name']} ({p.get('type')}){mark}")
        if len(projects) > _MAX_LISTED:
            lines.append(f"… and {len(projects) - _MAX_LISTED} more (any name can be used).")
    if data.get("suggested_name"):
        lines.append(f"New project: {data['suggested_name']} (or any other new name)")
    return "\n".join(lines)


# --- CLI ------------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="project.py", description=TOOL)
    parser.add_argument("--cwd", help="the session folder (default: the current directory)")
    parser.add_argument("--session", help="the session id")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("status", "list"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    sub.add_parser("start")
    p_use = sub.add_parser("use")
    p_use.add_argument("name", nargs="?")
    p_use.add_argument("--new", action="store_true")
    p_use.add_argument("--none", action="store_true", help="use coding mode (no project memory)")
    p_use.add_argument("--remember", action="store_true", default=None, help="remember this project for this folder")
    p_use.add_argument("--session-only", action="store_true", help="switch for this session only")
    p_use.add_argument("--json", action="store_true")
    sub.add_parser("refresh")
    ns = parser.parse_args(argv)
    cwd = ns.cwd or os.getcwd()
    try:
        if ns.cmd == "status":
            resolved = resolve(cwd, ns.session)
            print(json.dumps(context_resolution(resolved, cwd, ns.session)) if ns.json
                  else message(resolved))
            return 0 if resolved else 2
        if ns.cmd == "start":
            resolved = start(cwd, ns.session)
            spawn_refresh(cwd)
            print(json.dumps({"resolution": context_resolution(resolved, cwd, ns.session),
                              "message": message(resolved), "command": command_name()}))
            return 0
        if ns.cmd == "list":
            data = options(cwd, ns.session)
            print(json.dumps(data, indent=1) if ns.json else format_options(data) + "\n\n" +
                  persistence_instructions(file_persistence(cwd, ns.session)))
        elif ns.cmd == "use":
            remember = False if ns.session_only else (True if ns.remember else None)
            resolved = use(cwd, ns.session, ns.name, new=ns.new, none=ns.none, remember=remember)
            if ns.json:
                print(json.dumps(context_resolution(resolved, cwd, ns.session)))
            elif resolved.type == TYPE_NONE:
                print("This session is now in coding mode (no project memory attached).")
            else:
                where = f" ({_short(resolved.dot_path)})" if resolved.dot_path else ""
                print(f"This session now uses project {resolved.name}{where}.")
            if not ns.json:
                print(persistence_instructions(file_persistence(cwd, ns.session)))
        elif ns.cmd == "refresh":
            try:
                refresh()
            except Exception as exc:  # noqa: BLE001 — background refresh: quiet, fail open
                capture._debug(f"project refresh failed: {type(exc).__name__}")
    except ProjectError as exc:
        hint = exc.data.get("suggested_name")
        sys.stderr.write(f"{TOOL} failed: {exc}" + (f" (try {hint})" if hint else "") + "\n")
        return 1
    except gbrain_client.GbrainError as exc:
        sys.stderr.write(f"{TOOL} failed: {exc}\n")
        return 1
    except (ValueError, DotFileInvalid) as exc:
        sys.stderr.write(f"project: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
