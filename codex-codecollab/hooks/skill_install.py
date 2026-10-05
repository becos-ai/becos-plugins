#!/usr/bin/env python3
"""Auto-install the `/backfill` Codex skill on plugin load (called from codex_capture.py SessionStart).

Codex surfaces user capabilities as **skills** — a directory ``~/.codex/skills/<name>/SKILL.md`` with
frontmatter (name/description) + instructions the model follows. There is no plugin "load" event that
runs arbitrary code, and no deterministic shell-execution slash command (unlike Opencode's ``!`cmd```
templates), so the backfill *slash* surface is model-mediated: the skill tells the agent to run the
vendored ``cx_backfill.py`` and show its output. The dependable path stays the terminal CLI
(``codecollab-cx backfill``); this just makes it reachable from inside a Codex chat, one-step.

We bake the ABSOLUTE plugin path + an env-sourcing ``bash -lc`` wrapper (the same one hooks.json uses)
into the installed SKILL.md, so a Finder-launched minimal PATH can't break it. Idempotent: the full
expected SKILL.md is rendered on every call and the file is rewritten only when the installed content
differs from it byte-for-byte — so a plugin upgrade (new version-pinned cache root) or any template
change regenerates it, and an unchanged install is never touched. The version marker identifies the
file as ours (legacy-migration safety); it does not by itself gate regeneration. Fail-open: any error
is swallowed so a capture hook is never blocked.
"""
from __future__ import annotations

import os
from pathlib import Path

TEMPLATE_VERSION = 2
MARKER = f"codecollab-backfill-skill v{TEMPLATE_VERSION}"


def _skills_root() -> Path:
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return Path(home) / "skills"


def _migrate_old_skill(old_name: str, marker_prefix: str) -> None:
    """Remove a pre-namespace skill dir (e.g. ~/.codex/skills/login) — but only if it's OURS (its
    SKILL.md carries our marker), never a same-named skill from another plugin. Codex skills are keyed
    by directory name and have no plugin namespace, so we prefix ours `codecollab-` to avoid clobbering
    or being clobbered. Fail-open."""
    try:
        old = _skills_root() / old_name / "SKILL.md"
        if old.exists() and marker_prefix in old.read_text("utf-8"):
            old.unlink()
            try:
                old.parent.rmdir()   # drop the now-empty dir
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — never block a capture hook
        pass


def _render(plugin_root: Path) -> str:
    script = plugin_root / "deliver" / "cx_backfill.py"
    # Source the hook env (VONIC_BECOS_URL / _TOKEN_CMD / AUTHOR_NAME) then run the core, exactly as
    # hooks.json does — so auth works regardless of the shell Codex launches the tool from.
    run = ('bash -lc \'set -a; [ -f "$HOME/.codex/codecollab.env" ] && '
           '. "$HOME/.codex/codecollab.env" >/dev/null 2>&1; set +a; '
           f'exec python3 "{script}"')
    return f"""---
name: codecollab-backfill
description: Backfill pre-existing Codex sessions into becos memory. Use when the user asks to backfill, ingest, or import past/existing Codex sessions or history into memory. Dry-run first; only deliver after the user confirms.
---
<!-- {MARKER} -->

# Backfill past Codex sessions into memory

codecollab captures Codex sessions going forward. This sweeps sessions that happened BEFORE the
plugin was installed (or that you will not reopen) into the tenant's becos memory, through the same
path live capture uses. It reads only the clean user-prompt + final-assistant-reply layer of each
session — never reasoning, patches, or tool output.

## How to run it

Run the backfill core in the shell. **Dry run first — never deliver until the user has seen the dry
run and said yes.**

1. Dry run (lists what WOULD be ingested; ships nothing):

   ```
   {run} $ARGUMENTS'
   ```

2. Show the user the command's output **verbatim** — the full per-repository table, the session ids,
   and the turn counts. Do NOT summarize it to just totals; the user needs the session list to decide.

3. Only if the user then explicitly asks to ingest, re-run with `--deliver --async` appended (add
   `--project <repo>` or `--session <id>` to narrow, `--watch` to follow status):

   ```
   {run} --deliver --async $ARGUMENTS'
   ```

## Notes

- Repository memory only ingests repo-scoped work; sessions run outside a git repo (e.g. Codex
  desktop scratch dirs) are listed but skipped — say so if the user asks where a session went.
- Delivery is idempotent and resumable (stable campaign batch + local manifest + server-side dedup),
  so re-running is safe.
- The dependable path is the same command in a terminal: `codecollab-cx backfill [--deliver --async]`.
"""


def _write_if_changed(dest: Path, rendered: str) -> None:
    """Write ``rendered`` to ``dest`` unless it already holds exactly that content. Raises on I/O
    errors — callers are fail-open."""
    if dest.exists() and dest.read_text("utf-8") == rendered:
        return  # current — nothing to do
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered, "utf-8")


def ensure_backfill_skill(plugin_root: Path) -> None:
    """Write ~/.codex/skills/codecollab-backfill/SKILL.md if missing or its content differs from the
    current rendering (e.g. the plugin root moved to a new cache version). Fail-open."""
    try:
        _migrate_old_skill("backfill", "codecollab-backfill-skill")  # drop the un-namespaced skill
        rendered = _render(plugin_root)
        _write_if_changed(_skills_root() / "codecollab-backfill" / "SKILL.md", rendered)
    except Exception:  # noqa: BLE001 — never block a capture hook
        pass


LOGIN_TEMPLATE_VERSION = 3
LOGIN_MARKER = f"codecollab-login-skill v{LOGIN_TEMPLATE_VERSION}"


def _render_login(plugin_root: Path) -> str:
    """The `/login` skill — the in-chat surface for the WHOLE sign-in, first run included.

    It drives `bin/codecollab-cx login`, not the vendored `connect.py` underneath it. That matters:
    `connect.py` writes only the gateway session (`gateway-auth.json`), so a skill-only sign-in used
    to look like it succeeded while `codecollab.env` and the mint script were never written — leaving
    capture and recall silently dead. `codecollab-cx login` writes the mint script + env AND starts
    the browser flow, so the in-chat path is now complete on a virgin machine.

    No gateway/becos URL is passed: the CLI carries the production defaults, so the user supplies
    nothing. The plugin root is baked in absolutely and the file is re-written whenever its rendered
    content changes — a new plugin root (cache version) or a template edit — so the version-pinned
    cache path never reaches the user and never goes stale. Env-sourcing wrapper is the
    same one hooks.json uses — the `[ -f ]` guard makes it a no-op on a first run, when there is no
    env file yet."""
    cli = plugin_root / "bin" / "codecollab-cx"
    run = ('bash -lc \'set -a; [ -f "$HOME/.codex/codecollab.env" ] && '
           '. "$HOME/.codex/codecollab.env" >/dev/null 2>&1; set +a; '
           f'exec python3 "{cli}"')
    return f"""---
name: codecollab-login
description: Sign CodeCollab in through the browser (no email, OTP, password, or URLs to supply). Use when the user asks to log in, sign in, authenticate, connect, or set up codecollab/becos, or when capture reports an expired session. Two steps — start, then finish with the UUID the browser shows.
---
<!-- {LOGIN_MARKER} -->

# Sign CodeCollab in through the browser

Sign the installed CodeCollab plugin in — **first-time setup and re-authentication both**. This runs
the plugin's own CLI, which writes the token-mint script and the hook env AND starts the browser
flow, so nothing else has to be run in a terminal.

Never ask the user for an email, password, OTP, token, gateway URL, or connector URL — none are
needed. Never inspect or print anything under `~/.cache/codecollab`, and never print a token,
verifier, or pending-state contents.

## How to run it

1. If the user gave **no** authorization code, start sign-in and show its output verbatim (a browser
   URL and the next step — no secret):

   ```
   {run} login'
   ```

   **If it exits with `no author identity`**, that is expected on a machine with no git identity, and
   nothing was written. Ask the user how their work should be attributed in memory (a name or email —
   the same one they use on their other machines and tools), then re-run with it:

   ```
   {run} login --author <value>'
   ```

2. If the user provided **exactly one UUID**, finish the sign-in:

   ```
   {run} login <UUID>'
   ```

   Report the success or the safe error the script prints, including its next steps.

3. For anything else, do not run the script — tell the user the two forms are "log in" (starts the
   browser flow) and giving the UUID shown after authorizing (finishes it).

## Notes

- The gateway and connector URLs are built into the plugin; pass `--gateway` / `--becos-url` **only**
  if the user explicitly says they are on a self-hosted deployment.
- Sign-in has to be finished reasonably promptly — the browser authorization expires after about ten
  minutes. If it has, just start over from step 1.
- If capture still does nothing after a successful sign-in, the hooks are most likely untrusted: the
  user needs to run `/hooks` in the Codex CLI, trust the codecollab entries, and start a fresh task.
"""


TEAM_TEMPLATE_VERSION = 1
TEAM_MARKER = f"codecollab-team-skill v{TEAM_TEMPLATE_VERSION}"


def _render_team(plugin_root: Path) -> str:
    """The `codecollab-team` skill: team-activity questions answered by the server's
    `vonic_team_activity` through the vendored ``team_activity.py``.

    The question goes on stdin through a quoted heredoc (``-``), never into the command line, so
    quotes or ``$(...)`` in it stay text. Same env-sourcing wrapper as the other skills, so the
    script finds the becos URL and token-mint command."""
    script = plugin_root / "deliver" / "team_activity.py"
    run = ('bash -lc \'set -a; [ -f "$HOME/.codex/codecollab.env" ] && '
           '. "$HOME/.codex/codecollab.env" >/dev/null 2>&1; set +a; '
           f'exec python3 "{script}" -\'')
    return f"""---
name: codecollab-team
description: Answer questions about what a person or the team did, decided or changed, or who is working on what (e.g. "what did nikhil do today?", "changes in becos-memforest this week", "who is working on vonic-agent?"), from CodeCollab's captured coding sessions across the team's repositories. Use this instead of git log for team-activity questions.
---
<!-- {TEAM_MARKER} -->

# Team activity

Answer the user's team-activity question from CodeCollab's captured coding sessions (facts and
decisions with their author, repository and time — not git commits).

## How to run it

1. If there is no question, ask what the user wants to know (a person, a repository, a topic, or a
   time range) and stop.
2. Run the script with the user's question **verbatim on stdin** — never interpolate it into the
   command line:

   ```
   {run} <<'CODECOLLAB_QUESTION'
   <the question, verbatim>
   CODECOLLAB_QUESTION
   ```

3. Answer **only** from its output. The first line states the filters used (people, repositories,
   time window, topic): repeat that scope in one short line, then summarise the activity, grouped by
   person or day as the output is, keeping author emails and repositories, and cite ids where a
   claim rests on one entry. Do not add items from git, files or memory unless the user asks; if you
   do, label them as coming from git.
4. If nothing matched, say so and suggest widening the window or naming a person or repository. If
   it starts with `Error:` or the command fails, report that memory is unavailable (for an
   authentication error, suggest the `codecollab-login` skill).
"""


def ensure_team_skill(plugin_root: Path) -> None:
    """Write ~/.codex/skills/codecollab-team/SKILL.md if missing or its content differs from the
    current rendering. Fail-open."""
    try:
        _write_if_changed(_skills_root() / "codecollab-team" / "SKILL.md", _render_team(plugin_root))
    except Exception:  # noqa: BLE001 — never block a capture hook
        pass


def ensure_login_skill(plugin_root: Path) -> None:
    """Write ~/.codex/skills/codecollab-login/SKILL.md if missing or its content differs from the
    current rendering (e.g. the plugin root moved to a new cache version). Fail-open."""
    try:
        _migrate_old_skill("login", "codecollab-login-skill")  # drop the un-namespaced skill
        rendered = _render_login(plugin_root)
        _write_if_changed(_skills_root() / "codecollab-login" / "SKILL.md", rendered)
    except Exception:  # noqa: BLE001 — never block a capture hook
        pass
