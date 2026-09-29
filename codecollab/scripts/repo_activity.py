#!/usr/bin/env python3
"""Which git checkouts did this turn touch, and how?

Reconstructed, not observed. Claude Code's transcript already records every `tool_use` block
along with the `cwd` it ran in, so a turn's repository activity can be rebuilt from a file that
is on disk anyway — no filesystem watchers, no `PostToolUse` hook, no new event wiring.

The turn is bracketed by the two hooks that already exist:

  * `UserPromptSubmit` -> ``mark_turn_start`` stamps the transcript's byte offset (recall.py).
  * `Stop`             -> ``report`` reads only what was appended since, renders, and clears
                          the marker (capture.py).

STRICTLY LOCAL. This computes and prints a report on this machine; it changes nothing about
what leaves it. v0.12.0 removed a `repositoriesTouched` field from the delivered payload
because the receiving models are `extra="forbid"` and a client-side key addition must land
server-first (docs/DECISIONS.md) — that constraint is unchanged, and read-only paths must
never reach the per-turn `files` list, which ships as `metadata.event.changed_files`.

Classification is deliberately conservative: a repo is reported as *written* only when a
mutating tool or a clearly mutating command is visible in the transcript. Build and test
runners (`make`, `npm`, `pytest`, …) commonly modify a tree, but the plugin cannot observe
that — claiming it would be fabrication, so they count as reads.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — reuse _git, the cache dir, and the human-prompt test

_READ = "read"
_WRITE = "write"
_READ_WRITE = "read_write"          # only ever an `access` value, never an access *mode*
_DETACHED = "(detached)"
_LABELS = {_READ: "read only", _WRITE: "write only", _READ_WRITE: "read + write"}

# capture._EDIT_TOOLS is the write set; these are the read-only ones worth attributing.
_READ_TOOLS = {"Read", "Glob", "Grep", "NotebookRead"}
_PATH_KEYS = ("file_path", "notebook_path", "path")

# Command chaining. Splitting here is naive by design: a segment we mis-split degrades to
# "read of the cwd repo", never to a fabricated write.
_SEGMENT_RE = re.compile(r"&&|\|\||[;|\n]")
# `>` / `>>` but NOT `>&2` / `2>&1` — an fd dup is not a file write.
_REDIRECT_RE = re.compile(r"^>>?(?!&)")

# git subcommands that change the repository. Listing forms dominate `branch`/`tag`/`fetch`,
# so those stay reads rather than over-claiming.
_GIT_WRITE_SUBS = {
    "add", "am", "apply", "checkout", "cherry-pick", "clean", "commit", "init", "merge",
    "mv", "pull", "push", "rebase", "reset", "restore", "revert", "rm", "stash", "switch",
    "worktree",
}
# …and the same over-claiming one level deeper: these subcommands are containers whose FIRST
# argument decides. The mapping holds their read-only forms. `""` is the bare-subcommand case:
# bare `git worktree` only prints usage, while bare `git stash` IS `stash push` — a real write.
_GIT_READ_FORMS = {
    "stash": {"list", "show"},
    "worktree": {"list", ""},
}
_SHELL_WRITE_CMDS = {
    "chmod", "chown", "cp", "dd", "install", "ln", "mkdir", "mv", "patch", "rm", "rmdir",
    "tee", "touch", "truncate",
}

_HOME = os.path.expanduser("~")


# ── per-turn marker ──────────────────────────────────────────────────────────

def _turns_dir() -> str:
    path = os.path.join(capture._cache_dir(), "turns")
    os.makedirs(path, exist_ok=True)
    return path


def _marker_path(session_id: str) -> str:
    return os.path.join(_turns_dir(), f"{capture._slugify(session_id) or 'session'}.json")


def _read_marker(session_id: str) -> dict:
    try:
        with open(_marker_path(session_id), encoding="utf-8") as fh:
            marker = json.load(fh)
        return marker if isinstance(marker, dict) else {}
    except (OSError, ValueError):
        return {}


def _clear_marker(session_id: str) -> None:
    try:
        os.remove(_marker_path(session_id))
    except OSError:
        pass


def mark_turn_start(payload: dict) -> None:
    """UserPromptSubmit: stamp the transcript's current byte length as this turn's start.

    Whether Claude Code has flushed the prompt line by the time this hook runs does not matter:
    every assistant `tool_use` entry for the turn is appended *after* either position.
    """
    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    if not (session_id and transcript):
        return
    try:
        offset = os.path.getsize(transcript)
    except OSError:
        offset = 0
    marker = {"session_id": session_id, "transcript_path": transcript,
              "offset": offset, "ts": capture._iso_now()}
    try:
        with open(_marker_path(session_id), "w", encoding="utf-8") as fh:
            json.dump(marker, fh)
    except OSError as exc:
        capture._debug(f"repo-activity: cannot write turn marker: {exc}")


def prune_markers(max_age_days: int = 7) -> None:
    """SessionStart: drop markers left by sessions that never reached a Stop hook."""
    cutoff = time.time() - max_age_days * 86400
    try:
        names = os.listdir(_turns_dir())
    except OSError:
        return
    for name in names:
        path = os.path.join(_turns_dir(), name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


# ── turn boundary ────────────────────────────────────────────────────────────

def _last_prompt_offset(transcript_path: str) -> int:
    """Byte offset at which the most recent *human* prompt line begins.

    The fallback boundary, used when the marker is missing or stale. It expresses the same
    turn edge as capture's `turns_recorded` watermark without needing any state. Sidechain
    entries are skipped: a subagent's prompt is a `type: "user"` line too, and treating one as
    the turn start would clip the parent turn's activity.
    """
    pos = start = 0
    try:
        with open(transcript_path, "rb") as fh:
            for raw in fh:
                line_start, pos = pos, pos + len(raw)
                try:
                    entry = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(entry, dict) or entry.get("isSidechain") or entry.get("isMeta"):
                    continue
                if capture._is_human_prompt(entry):
                    start = line_start
    except OSError as exc:
        capture._debug(f"repo-activity: cannot scan transcript: {exc}")
        return 0
    return start


def _turn_start_offset(session_id: str, transcript_path: str) -> int:
    """The marker when it still describes this transcript, else the last-prompt fallback.

    The marker is rejected when it is missing (hook not registered, older cached build, first
    turn after an upgrade), when the session or transcript changed under it (`--resume`), or
    when the file is now SHORTER than the recorded offset (compaction rewrote it).
    """
    marker = _read_marker(session_id)
    if marker.get("session_id") == session_id and marker.get("transcript_path") == transcript_path:
        try:
            offset = int(marker.get("offset") or 0)
            size = os.path.getsize(transcript_path)
        except (OSError, TypeError, ValueError):
            offset, size = -1, -1
        if 0 <= offset <= size:
            return offset
    return _last_prompt_offset(transcript_path)


# ── path extraction ──────────────────────────────────────────────────────────

def _abspath(path: str, cwd: str) -> str:
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd or os.getcwd(), path)
    return os.path.normpath(path)


def _path_args(args: list[str], base: str) -> list[str]:
    """Arguments that are confidently paths: they contain a separator, or they name something
    that exists under `base`. A bare word that happens to be a filename is a harmless
    over-match — it resolves to the same repo as `base` anyway."""
    paths = []
    for arg in args:
        if not arg or arg.startswith("-"):
            continue
        resolved = _abspath(arg, base)
        if os.sep in arg or os.path.exists(resolved):
            paths.append(resolved)
    return paths


def _split_redirects(tokens: list[str]) -> tuple[list[str], bool]:
    """Strip `>`/`>>` operators, keeping their targets as ordinary path arguments."""
    out: list[str] = []
    writes = False
    for tok in tokens:
        match = _REDIRECT_RE.match(tok)
        if not match:
            out.append(tok)
            continue
        writes = True
        rest = tok[match.end():]
        if rest:
            out.append(rest)
    return out, writes


def _classify(tokens: list[str], base: str) -> tuple[str, list[str]]:
    """One command segment -> (mode, paths). Unrecognised commands are reads of `base`."""
    tokens, redirected = _split_redirects(tokens)
    if not tokens:
        return (_WRITE if redirected else _READ), [base]
    head = os.path.basename(tokens[0])
    args = tokens[1:]
    mode = _WRITE if redirected else _READ

    if head == "git":
        # `git -C <dir>` relocates the command; the subcommand alone decides the mode.
        targets: list[str] = []
        sub = ""
        i = 0
        while i < len(args):
            if args[i] == "-C" and i + 1 < len(args):
                targets.append(_abspath(args[i + 1], base))
                i += 2
                continue
            if args[i].startswith("-"):
                i += 1
                continue
            sub = args[i]
            break
        if sub in _GIT_WRITE_SUBS:
            verb = next((a for a in args[i + 1:] if not a.startswith("-")), "")
            if verb not in _GIT_READ_FORMS.get(sub, ()):
                mode = _WRITE
        return mode, targets or [base]

    if head in _SHELL_WRITE_CMDS:
        mode = _WRITE
    elif head in ("sed", "perl", "ruby") and any(a.startswith("-i") for a in args):
        mode = _WRITE
    return mode, _path_args(args, base) or [base]


def _bash_accesses(command: str, cwd: str) -> list[tuple[str, str]]:
    """Repository access inferred from a shell command — cwd, `cd`, `git -C`, explicit paths.

    Never claims a write the transcript does not show. Anything a script does internally is
    invisible here and is not guessed at.
    """
    accesses: list[tuple[str, str]] = []
    base = cwd or os.getcwd()
    for segment in _SEGMENT_RE.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if not tokens:
            continue
        if os.path.basename(tokens[0]) == "cd":
            if len(tokens) > 1:  # moves the base for the rest of the chain
                base = _abspath(tokens[1], base)
            continue
        mode, paths = _classify(tokens, base)
        accesses.extend((path, mode) for path in paths)
    return accesses


def _accesses(name: str, inp: dict, cwd: str) -> list[tuple[str, str]]:
    """One `tool_use` block -> the paths it touched and how."""
    if not isinstance(inp, dict):
        return []
    if name == "Bash":
        return _bash_accesses(str(inp.get("command") or ""), cwd)
    if name in capture._EDIT_TOOLS:
        mode = _WRITE
    elif name in _READ_TOOLS:
        mode = _READ
    else:
        return []  # Task / Skill / MCP / … — no path we can attribute with confidence
    for key in _PATH_KEYS:
        value = inp.get(key)
        if value:
            return [(_abspath(str(value), cwd), mode)]
    # Glob and Grep search the cwd when given no path; an edit tool with no path is unusable.
    return [(_abspath(cwd, cwd), mode)] if mode == _READ else []


def _scan(transcript_path: str, offset: int, default_cwd: str) -> list[tuple[str, str]]:
    """Tool activity appended since `offset`, deduped by `tool_use.id`.

    Sidechain entries are included on purpose: a subagent reading or editing a repo is still
    this turn touching that repo.
    """
    accesses: list[tuple[str, str]] = []
    seen: set[str] = set()
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(offset)
            raw_lines = fh.readlines()
    except OSError as exc:
        capture._debug(f"repo-activity: cannot read transcript: {exc}")
        return accesses
    for raw in raw_lines:
        try:
            entry = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue  # a partial first line after a mid-line offset, or a torn write
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        cwd = entry.get("cwd") or default_cwd
        content = (entry.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_use"):
                continue
            uid = block.get("id")
            if uid:
                if uid in seen:
                    continue  # the same operation re-emitted (retry, resumed transcript)
                seen.add(uid)
            accesses.extend(_accesses(block.get("name") or "", block.get("input") or {}, cwd))
    return accesses


# ── resolution + aggregation ─────────────────────────────────────────────────

def _nearest_dir(path: str) -> str:
    """Closest EXISTING directory at or above `path` — a Write may name a file that does not
    exist yet, and `git -C` needs a real directory to run in."""
    directory = path if os.path.isdir(path) else os.path.dirname(path)
    while directory and not os.path.isdir(directory):
        parent = os.path.dirname(directory)
        if parent == directory:
            return ""
        directory = parent
    return directory


class _Resolver:
    """Git lookups for one turn, cached: one `rev-parse` per directory, one branch and one
    origin-slug per root. The slug is what the wire form carries — see `_structure`."""

    def __init__(self) -> None:
        self._roots: dict[str, str | None] = {}
        self._branches: dict[str, str | None] = {}
        self._slugs: dict[str, str] = {}

    def root(self, path: str) -> str | None:
        directory = _nearest_dir(path)
        if directory not in self._roots:
            self._roots[directory] = (
                capture._git(directory, "rev-parse", "--show-toplevel") if directory else None
            )
        return self._roots[directory]

    def branch(self, root: str) -> str | None:
        if root not in self._branches:
            # rev-parse --abbrev-ref HEAD, already None on a detached HEAD.
            self._branches[root] = capture._branch_or_none(root)
        return self._branches[root]

    def slug(self, root: str) -> str:
        """``org/repo`` from origin, falling back to the directory name — the same identity
        `_deliver_becos` sends as `repo_name`, and never a filesystem path."""
        if root not in self._slugs:
            self._slugs[root] = capture._repo_slug(root) or os.path.basename(root)
        return self._slugs[root]


def _aggregate(accesses: list[tuple[str, str]],
               resolver: _Resolver) -> tuple[dict[tuple[str, str | None], dict], int]:
    """Fold accesses into one entry per (repository root, branch). Both flags are kept; the
    write-wins promotion happens at render time. Paths outside any work tree are counted, never
    named."""
    repos: dict[tuple[str, str | None], dict] = {}
    orphans: set[str] = set()
    for path, mode in accesses:
        root = resolver.root(path)
        if not root:
            orphans.add(path)
            continue
        flags = repos.setdefault((root, resolver.branch(root)), {_READ: False, _WRITE: False})
        flags[mode] = True
    return repos, len(orphans)


def _display(root: str) -> str:
    if _HOME and (root == _HOME or root.startswith(_HOME + os.sep)):
        return "~" + root[len(_HOME):]
    return root


def _access(flags: dict) -> str:
    if flags[_WRITE]:
        return _READ_WRITE if flags[_READ] else _WRITE
    return _READ


def _structure(repos: dict[tuple[str, str | None], dict], orphans: int,
               resolver: _Resolver) -> dict | None:
    """One turn's activity as data, written repos first then alphabetically by displayed path.

    `repo` is an origin slug and is the only identity that may leave the machine; `path` is the
    local display form and must stay in the local report and log — shipping it would leak this
    machine's directory layout for checkouts the delivered event is not even scoped to.
    """
    if not repos:
        return None   # orphans alone are not a repository report
    entries = [
        {"repo": resolver.slug(root), "branch": branch,
         "access": _access(flags), "path": _display(root)}
        for (root, branch), flags in repos.items()
    ]
    entries.sort(key=lambda e: (e["access"] == _READ, e["path"]))
    return {"repositories": entries, "unresolved_paths": orphans}


def resolve_accesses(accesses: list[dict] | None, default_cwd: str,
                     resolver: _Resolver | None = None) -> dict | None:
    """Resolve a privacy-reduced path-access list supplied by a non-Claude adapter.

    Adapters must discard raw tool arguments before this boundary and provide only
    ``{"path": str, "access": "read" | "write"}``. Local paths are used to discover git
    identities, then `_attach_repo_activity` strips them from wire metadata exactly as it does for
    transcript-derived activity.
    """
    safe: list[tuple[str, str]] = []
    for item in accesses or []:
        if not isinstance(item, dict) or item.get("access") not in {_READ, _WRITE, _READ_WRITE}:
            continue
        path = item.get("path")
        if isinstance(path, str) and path.strip():
            safe.append((_abspath(path, default_cwd), item["access"]))
    if not safe:
        return None
    active_resolver = resolver or _Resolver()
    repos, orphans = _aggregate(safe, active_resolver)
    return _structure(repos, orphans, active_resolver)


def resolve_turn_accesses(turns: list[dict] | None, default_cwd: str) -> list[dict | None]:
    """Resolve every normalized turn with one shared git cache for the whole session."""
    resolver = _Resolver()
    return [
        resolve_accesses(turn.get("accesses"), default_cwd, resolver)
        if isinstance(turn, dict) else None
        for turn in (turns or [])
    ]


# ── capture-context path resolution (CMEM-support evidence) ───────────────────
#
# The normalizer emits deterministic evidence — tool_events + file_ops — carrying LOCAL absolute
# paths. Exactly like `resolve_accesses`, this reduces those local paths to `{repo, path}` refs
# (origin slug + repo-relative path) so the machine's directory layout never leaves it. Semantic
# CMEM fields (title, narrative, facts, …) are the downstream processor's job, never the plugin's.

_TOOL_ACCESSES = {"read", "write", "search", "execute"}
_FILE_OP_BUCKET = {
    "read": "files_read", "modified": "files_modified",
    "created": "files_created", "deleted": "files_deleted",
}
_OP_RANK = {"read": 0, "modified": 1, "deleted": 2, "created": 3}
_MAX_TOOL_NAME = 64
_MAX_STR = 64          # timestamps / status
_MAX_EVENT_PATHS = 50  # referenced paths per tool event
_MAX_TOOL_EVENTS = 300
_MAX_FILES = 500       # per aggregate bucket
_MAX_PATH_LEN = 1_024
_MAX_SYMBOLS = 100
_MAX_SYMBOL_LEN = 128


def _repo_ref(abspath: str, resolver: _Resolver, *, allow_dir: bool) -> dict | None:
    """One absolute local path -> ``{repo: <origin slug>, path: <repo-relative>}``, or ``None`` when
    the path is outside any work tree (never leaked as an absolute path). ``allow_dir`` keeps a
    directory ref (a search location) but drops the bare repo root from concrete file lists."""
    root = resolver.root(abspath)
    if not root:
        return None
    rel = os.path.relpath(abspath, root)
    if rel.startswith(".."):
        return None
    if rel == "." and not allow_dir:
        return None
    return {"repo": resolver.slug(root), "path": rel[:_MAX_PATH_LEN]}


def resolve_capture_paths(turn: dict, default_cwd: str,
                          resolver: _Resolver | None = None) -> dict:
    """Resolve one normalized turn's ``tool_events`` + ``file_ops`` to privacy-safe repo-relative
    refs. Returns ``{tool_events?, files_read?, files_modified?, files_created?, files_deleted?}``
    — only the keys that resolved to something. Never returns local absolute paths."""
    if not isinstance(turn, dict):
        return {}
    resolver = resolver or _Resolver()
    result: dict = {}

    events: list[dict] = []
    for ev in turn.get("tool_events") or []:
        if not isinstance(ev, dict):
            continue
        tool = ev.get("tool")
        access = ev.get("access")
        if not isinstance(tool, str) or not tool.strip() or access not in _TOOL_ACCESSES:
            continue
        out: dict = {"order": ev.get("order", len(events)), "tool": tool[:_MAX_TOOL_NAME],
                     "access": access}
        for key in ("started_at", "completed_at", "status"):
            value = ev.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value[:_MAX_STR]
        refs: list[dict] = []
        for path in ev.get("paths") or []:
            if isinstance(path, str) and path.strip():
                ref = _repo_ref(_abspath(path, default_cwd), resolver, allow_dir=True)
                if ref and ref not in refs:
                    refs.append(ref)
        if refs:
            out["paths"] = refs[:_MAX_EVENT_PATHS]
        events.append(out)
    if events:
        result["tool_events"] = events[:_MAX_TOOL_EVENTS]

    # Bucket file operations, keeping the most informative op and summing deterministic diff stats.
    best: dict[tuple[str, str], tuple[str, dict, int, int]] = {}
    for op_ref in turn.get("file_ops") or []:
        if not isinstance(op_ref, dict):
            continue
        op = op_ref.get("op")
        path = op_ref.get("path")
        if op not in _OP_RANK or not isinstance(path, str) or not path.strip():
            continue
        ref = _repo_ref(_abspath(path, default_cwd), resolver, allow_dir=False)
        if not ref:
            continue
        key = (ref["repo"], ref["path"])
        additions = op_ref.get("additions")
        deletions = op_ref.get("deletions")
        additions = additions if isinstance(additions, int) and additions >= 0 else 0
        deletions = deletions if isinstance(deletions, int) and deletions >= 0 else 0
        if key in best:
            old_op, old_ref, old_additions, old_deletions = best[key]
            if _OP_RANK[op] < _OP_RANK[old_op]:
                op = old_op
            best[key] = (op, old_ref, old_additions + additions, old_deletions + deletions)
        else:
            best[key] = (op, ref, additions, deletions)
    buckets: dict[str, list[dict]] = {}
    changed_files: list[dict] = []
    for op, ref, additions, deletions in best.values():
        buckets.setdefault(_FILE_OP_BUCKET[op], []).append(ref)
        if op != "read":
            changed_files.append({**ref, "status": op, "additions": additions,
                                  "deletions": deletions})
    for bucket, refs in buckets.items():
        result[bucket] = refs[:_MAX_FILES]
    if changed_files:
        result["changed_files"] = changed_files[:_MAX_FILES]

    symbols: list[str] = []
    for symbol in turn.get("mentioned_symbols") or []:
        if (isinstance(symbol, str) and symbol and len(symbol) <= _MAX_SYMBOL_LEN
                and symbol not in symbols):
            symbols.append(symbol)
        if len(symbols) >= _MAX_SYMBOLS:
            break
    if symbols:
        result["mentioned_symbols"] = symbols
    return result


def resolve_turn_capture(turns: list[dict] | None, default_cwd: str) -> list[dict]:
    """Resolve every normalized turn's capture-context paths with one shared git cache."""
    resolver = _Resolver()
    return [
        resolve_capture_paths(turn, default_cwd, resolver) if isinstance(turn, dict) else {}
        for turn in (turns or [])
    ]


def render(activity: dict | None) -> str | None:
    """The human report. None when the turn touched no checkout."""
    if not activity:
        return None
    lines = ["Repositories touched this turn:"]
    for entry in activity["repositories"]:
        lines.append(f"- {entry['path']} | branch: {entry['branch'] or _DETACHED} "
                     f"| {_LABELS[entry['access']]}")
    if activity.get("unresolved_paths"):
        lines.append(f"- {activity['unresolved_paths']} path(s) outside any git repository")
    return "\n".join(lines)


def collect(payload: dict) -> dict | None:
    """Stop: the repos touched since this turn's start, as data, then clear the marker.

    Split from `report` because the structured form has to reach `capture._record_turns` BEFORE
    `_dispatch` spawns the detached deliverer that re-reads the buffer from disk.
    """
    session_id = payload.get("session_id") or ""
    transcript = payload.get("transcript_path") or ""
    if not transcript:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    try:
        offset = _turn_start_offset(session_id, transcript)
        resolver = _Resolver()
        repos, orphans = _aggregate(_scan(transcript, offset, cwd), resolver)
        return _structure(repos, orphans, resolver)
    finally:
        _clear_marker(session_id)


def report(payload: dict) -> str | None:
    """Stop: the rendered report. Kept for callers (and vendored copies) that only want text."""
    return render(collect(payload))


def for_paths(paths: list[str], default_cwd: str, mode: str = _WRITE) -> dict | None:
    """Repository activity for an EXPLICIT list of file paths.

    The transcript-scanning ``collect`` reconstructs accesses from Claude Code ``tool_use`` blocks.
    An adapter that already knows the files a turn touched — e.g. the Codex plugin, which reads them
    from the typed ``patch_apply_end`` event — skips that reconstruction and hands the paths here.
    ``mode`` is the access recorded for every path (writes by default; a typed patch event is a
    write). Returns the same ``{repositories, unresolved_paths}`` shape as ``collect`` so it flows
    through ``capture._attach_repo_activity`` identically, or ``None`` when nothing resolved to a
    work tree. Never raises — a report must not break delivery.
    """
    try:
        resolver = _Resolver()
        accesses = [(_abspath(str(p), default_cwd), mode) for p in paths if p]
        repos, orphans = _aggregate(accesses, resolver)
        return _structure(repos, orphans, resolver)
    except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
        capture._debug(f"repo-activity for_paths: {exc}")
        return None
