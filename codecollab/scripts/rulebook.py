#!/usr/bin/env python3
"""Rulebook: the rules your team stated to its coding agents, accepted by you (vonic_rulebook).

    python3 rulebook.py list [--cursor C] [--json]   accepted rules + numbered candidates
    python3 rulebook.py decide [--json]              stdin {"accept": [...], "reject": [...]}
    python3 rulebook.py refresh                      update the local cache (quiet; detached)
    python3 rulebook.py section                      print the cached session section
    python3 rulebook.py --cwd DIR <command>          run for the checkout at DIR (default: cwd)

becos_memforest groups the standing rules a team states to its coding agents (guardrails, coding
conventions, project decisions) per repository; each user accepts or rejects them for themselves.
``list`` shows the caller's accepted rules and the candidates; ``decide`` records choices. In
``accept``/``reject`` an entry may be a rule id, a candidate's number from the latest ``list``, or
``"all"`` (every candidate of the latest list). Accepted rules are cached per repository and
returned to the agent as a "## Rulebook" session section: ``instructions.py`` adds the cached
section at session start and after compaction and refreshes the cache in the background; when there
was no cache yet, ``recall.py`` injects the fresh section once on the first prompt.

The repository is the checkout's origin slug; its root ``AGENTS.md``, ``CLAUDE.md`` and
``.cursor/rules/*`` are sent (bounded) so rules they already state are skipped. Off by default:
``VONIC_CODECOLLAB_RULEBOOK=1`` turns the session section on (the command works either way).
``VONIC_RULEBOOK_TIMEOUT`` bounds each call (default 20 s). Exit codes: 0 ok, 1 the call failed,
2 usage or no repository. Everything that runs from a hook fails open.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — becos config + identity resolution
import connect  # noqa: E402 — atomic writes
import gbrain_client  # noqa: E402

TOOL = "vonic_rulebook"
_DEFAULT_TIMEOUT = 20.0
MAX_SECTION_RULES = 25
MAX_RULE_CHARS = 220
_MAX_FILES = 3
_MAX_FILE_CHARS = 20_000
_PENDING_MAX_AGE_S = 2 * 86_400
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")

SECTION_HEADER = "## Rulebook"
SECTION_PREAMBLE = (
    "Rules you accepted for this repository with the CodeCollab rulebook command "
    "(`/codecollab-rulebook`; Claude Code: `/codecollab:rulebook`). They are your own choices; "
    "teammates choose theirs. Follow them in this session. The repository's own AGENTS.md / "
    "CLAUDE.md win on conflict."
)


def enabled(env: dict | None = None) -> bool:
    """``VONIC_CODECOLLAB_RULEBOOK=1`` (off by default; only exactly ``1`` enables it)."""
    source = os.environ if env is None else env
    return (source.get("VONIC_CODECOLLAB_RULEBOOK") or "").strip() == "1"


def _timeout() -> float:
    try:
        value = float(os.environ.get("VONIC_RULEBOOK_TIMEOUT", "") or _DEFAULT_TIMEOUT)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


# --- repository + instruction files ----------------------------------------------------------------


def repo_context(cwd: str | None, session_id: object = None) -> tuple[str, str] | None:
    """``(org/repo slug, checkout root)`` for ``cwd``, or None outside a git checkout. With
    projects on: ``(project key, checkout root or "")`` for the session's project."""
    cwd = cwd or os.getcwd()
    if capture._projects_on():
        resolved = capture._project(lambda mod: mod.resolve(cwd, session_id))
        if resolved is None:
            return None
        root = (capture._git(cwd, "rev-parse", "--show-toplevel") or ""
                if resolved.type == "git-repo" and resolved.source == "git" else "")
        return resolved.key, root
    root = capture._git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None
    slug = capture._repo_slug(root)
    return (slug, root) if slug else None


def instruction_files(root: str) -> list[dict]:
    """The checkout's root AGENTS.md, CLAUDE.md and .cursor/rules/* (bounded; at most three)."""
    candidates = [os.path.join(root, "AGENTS.md"), os.path.join(root, "CLAUDE.md")]
    rules_dir = os.path.join(root, ".cursor", "rules")
    try:
        candidates += sorted(os.path.join(rules_dir, n) for n in os.listdir(rules_dir)
                             if n.endswith((".md", ".mdc")))
    except OSError:
        pass
    out: list[dict] = []
    for path in candidates:
        if len(out) >= _MAX_FILES:
            break
        try:
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(_MAX_FILE_CHARS + 1)
        except OSError:
            continue
        if not text.strip():
            continue
        out.append({"path": os.path.relpath(path, root), "text": text[:_MAX_FILE_CHARS],
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()})
    return out


# --- server call -----------------------------------------------------------------------------------


class RulebookError(RuntimeError):
    pass


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
    raise RulebookError("unexpected response from the server")


def call(arguments: dict) -> dict:
    url, token = capture._resolve_becos()
    result = gbrain_client.call_tool(url, token, TOOL, arguments, _timeout(),
                                     extra_headers=capture._becos_identity_headers())
    if result.get("isError"):
        raise RulebookError(_first_text(result) or "the server returned an error")
    data = _decode(result)
    if data.get("error"):
        raise RulebookError(str(data["error"]))
    return data


def _first_text(result: dict) -> str:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text") or "")[:300]
    return ""


# --- cache -----------------------------------------------------------------------------------------


def _cache_dir() -> str:
    path = os.path.join(capture._cache_dir(), "rulebook")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _cache_key(slug: str) -> str:
    """Per destination, signed-in user and repository (two logins on one machine stay apart)."""
    try:
        url = capture._resolve_becos_url()
    except Exception:  # noqa: BLE001 — no config: still a stable per-repo key
        url = ""
    who = capture._cached_author()
    return hashlib.sha256(f"{url}\x1f{who}\x1f{slug}".encode()).hexdigest()[:32]


def cache_path(slug: str) -> str:
    return os.path.join(_cache_dir(), f"{_cache_key(slug)}.json")


def read_cache(slug: str) -> dict | None:
    try:
        with open(cache_path(slug), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("accepted"), list) else None


def write_cache(slug: str, *, accepted: list | None = None,
                candidates: list | None = None) -> None:
    current = read_cache(slug) or {"slug": slug, "accepted": [], "candidates": []}
    if accepted is not None:
        current["accepted"] = [r for r in accepted if isinstance(r, dict)][:MAX_SECTION_RULES]
    if candidates is not None:
        current["candidates"] = [{"id": c.get("id"), "text": c.get("text")}
                                 for c in candidates if isinstance(c, dict)]
    current["fetched_at"] = time.time()
    connect._atomic_write(cache_path(slug), current)


# --- formatting ------------------------------------------------------------------------------------


def _clip(text: str, limit: int = MAX_RULE_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_section(accepted: list) -> str:
    """The session section for ``accepted`` rules ("" when there are none)."""
    rules = [r for r in accepted if isinstance(r, dict) and r.get("text")][:MAX_SECTION_RULES]
    if not rules:
        return ""
    lines = [SECTION_HEADER, SECTION_PREAMBLE]
    for n, rule in enumerate(rules, 1):
        paths = rule.get("paths") or []
        where = f" (files: {', '.join(str(p) for p in paths[:3])})" if paths else ""
        lines.append(f"{n}. {_clip(rule['text'])}{where} [{rule.get('id', '')}]")
    return "\n".join(lines)


def format_listing(slug: str, data: dict) -> str:
    accepted = data.get("accepted") or []
    candidates = data.get("candidates") or []
    counts = data.get("counts") or {}
    out = [f"Rulebook for {slug}: {len(accepted)} accepted, "
           f"{counts.get('candidates_total', len(candidates))} candidate(s), "
           f"{counts.get('covered', 0)} skipped because AGENTS.md/CLAUDE.md already state them."]
    out.append("")
    out.append("Accepted (in your sessions):" if accepted else "Accepted: none yet.")
    for rule in accepted:
        flag = " [updated since you accepted it]" if rule.get("updated") else ""
        out.append(f"- {_clip(rule.get('text'))}{flag} ({rule.get('id')})")
    out.append("")
    if candidates:
        out.append("Candidates (accept or reject by number or id):")
        for n, rule in enumerate(candidates, 1):
            who = rule.get("authors") or []
            meta = [f"stated {rule.get('statements', 1)}x"]
            if who:
                meta.append("by " + ", ".join(who[:3]) + (" …" if len(who) > 3 else ""))
            if rule.get("returning"):
                meta.append("restated since you rejected it")
            if rule.get("paths"):
                meta.append("files: " + ", ".join(rule["paths"][:3]))
            out.append(f"{n}. {_clip(rule.get('text'))} ({rule.get('id')}; {'; '.join(meta)})")
        if data.get("next_cursor"):
            out.append(f"More candidates: rulebook.py list --cursor {data['next_cursor']}")
    else:
        out.append("No new candidates.")
    return "\n".join(out)


# --- operations ------------------------------------------------------------------------------------


def list_rules(cwd: str | None, *, cursor: str | None = None) -> tuple[str, dict]:
    ctx = repo_context(cwd)
    if ctx is None:
        raise ValueError("not inside a git repository")
    slug, root = ctx
    args: dict = {"action": "list", "repository": slug,
                  "instruction_files": instruction_files(root) if root else []}
    if cursor:
        args["cursor"] = cursor
    data = call(args)
    write_cache(slug, accepted=data.get("accepted") or [],
                candidates=data.get("candidates") or [])
    return slug, data


def _resolve_ids(tokens: object, last: list) -> list[str]:
    if isinstance(tokens, str):
        tokens = [tokens]
    if not isinstance(tokens, list):
        return []
    ids: list[str] = []
    for token in tokens:
        token = str(token).strip()
        if not token:
            continue
        if token.lower() == "all":
            ids += [str(c.get("id")) for c in last if c.get("id")]
        elif token.isdigit():
            n = int(token)
            if 1 <= n <= len(last) and last[n - 1].get("id"):
                ids.append(str(last[n - 1]["id"]))
        else:
            ids.append(token)
    return list(dict.fromkeys(ids))


def decide(cwd: str | None, choices: dict) -> tuple[str, dict]:
    ctx = repo_context(cwd)
    if ctx is None:
        raise ValueError("not inside a git repository")
    slug, _root = ctx
    last = (read_cache(slug) or {}).get("candidates") or []
    needs_list = any(str(t).strip().lower() == "all" or str(t).strip().isdigit()
                     for key in ("accept", "reject")
                     for t in (choices.get(key) if isinstance(choices.get(key), list)
                               else [choices.get(key)]) if t is not None)
    if needs_list and not last:
        _slug, listing = list_rules(cwd)
        last = listing.get("candidates") or []
    accept = _resolve_ids(choices.get("accept"), last)
    reject = [i for i in _resolve_ids(choices.get("reject"), last) if i not in accept]
    if not (accept or reject):
        raise ValueError("nothing to record: give rule ids, candidate numbers or 'all'")
    data = call({"action": "decide", "repository": slug, "accept": accept, "reject": reject})
    write_cache(slug, accepted=data.get("accepted") or [], candidates=[])
    return slug, data


def format_decision(slug: str, data: dict) -> str:
    rec = data.get("recorded") or {}
    accepted = data.get("accepted") or []
    lines = [f"Recorded for {slug}: accepted {len(rec.get('accepted') or [])}, "
             f"rejected {len(rec.get('rejected') or [])}"
             + (f", unknown ids ignored: {', '.join(rec['unknown'])}" if rec.get("unknown")
                else "") + "."]
    section = format_section(accepted)
    if section:
        lines += ["", "These rules apply from now on in this session and in your next sessions:",
                  "", section]
    else:
        lines.append("You have no accepted rules for this repository.")
    return "\n".join(lines)


# --- hooks (session start, first prompt) -------------------------------------------------------


def session_section(cwd: str | None) -> str:
    """The cached section for ``cwd``'s repository ("" when off, uncached or outside a repo)."""
    if not enabled():
        return ""
    ctx = repo_context(cwd)
    if ctx is None:
        return ""
    cached = read_cache(ctx[0])
    return format_section(cached["accepted"]) if cached else ""


def _pending_path(session_id: object) -> str | None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        return None
    return os.path.join(_cache_dir(), f"pending-{session_id}")


def spawn_refresh(cwd: str | None, session_id: object = None, *, pending: bool = False) -> bool:
    """Refresh the cache in a detached child; with ``pending`` the first prompt of
    ``session_id`` will inject the fresh section once (there was no cache to inject now)."""
    if not enabled():
        return False
    marker = _pending_path(session_id) if pending else None
    if marker:
        _sweep_pending()
        connect._atomic_write(marker, {"cwd": cwd, "written_at": time.time()})
    try:
        child = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "refresh"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True, cwd=cwd or None,
        )
        child.stdin.write(json.dumps({"cwd": cwd}).encode("utf-8"))
        child.stdin.close()
    except OSError:
        return False
    return True


def _sweep_pending() -> None:
    cutoff = time.time() - _PENDING_MAX_AGE_S
    try:
        for entry in os.scandir(_cache_dir()):
            if entry.name.startswith("pending-") and entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
    except OSError:
        pass


def claim_pending(session_id: object, cwd: str | None) -> str:
    """The fresh section to inject once on this session's first prompt, else ""."""
    marker = _pending_path(session_id)
    if not marker or not os.path.exists(marker) or not enabled():
        return ""
    section = session_section(cwd)
    if not section:
        return ""     # refresh not finished yet (or no rules): keep the marker for the next prompt
    try:
        os.remove(marker)   # claimed: injected exactly once
    except OSError:
        return ""
    return section


# --- CLI -----------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rulebook.py", description=TOOL)
    parser.add_argument("--cwd", help="the repository checkout (default: the current directory)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("--cursor")
    p_list.add_argument("--json", action="store_true")
    p_decide = sub.add_parser("decide")
    p_decide.add_argument("--json", action="store_true")
    sub.add_parser("refresh")
    sub.add_parser("section")
    ns = parser.parse_args(argv)
    cwd = ns.cwd or os.getcwd()
    try:
        if ns.cmd == "list":
            slug, data = list_rules(cwd, cursor=ns.cursor)
            print(json.dumps(data, indent=1) if ns.json else format_listing(slug, data))
        elif ns.cmd == "decide":
            raw = sys.stdin.read().strip()
            choices = json.loads(raw) if raw else {}
            if not isinstance(choices, dict):
                raise ValueError('stdin must be {"accept": [...], "reject": [...]}')
            slug, data = decide(cwd, choices)
            print(json.dumps(data, indent=1) if ns.json else format_decision(slug, data))
        elif ns.cmd == "refresh":
            raw = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
            target = (json.loads(raw).get("cwd") if raw else None) or cwd
            try:
                list_rules(target)
            except Exception as exc:  # noqa: BLE001 — background refresh: quiet, fail open
                capture._debug(f"rulebook refresh failed: {type(exc).__name__}")
        elif ns.cmd == "section":
            print(session_section(cwd) if enabled() else format_section(
                (read_cache((repo_context(cwd) or ("", ""))[0]) or {}).get("accepted") or []))
    except (ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"rulebook: {exc}\n")
        return 2
    except (RulebookError, gbrain_client.GbrainError) as exc:
        sys.stderr.write(f"{TOOL} failed: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except gbrain_client.GbrainError as exc:   # misconfiguration (no url/token)
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)
