---
description: Backfill pre-existing Claude Code sessions into becos memory (dry-run first, then confirm)
argument-hint: "[project] [session-id ...] [--async] [--campaign name] [--watch]"
---

Ingest the user's **pre-existing** Claude Code session transcripts into becos memory. codecollab
captures sessions going forward and fully backfills any session the user *reopens* (the Stop hook
reads the whole transcript); this command sweeps the sessions they will *not* reopen.

Parse `$ARGUMENTS`: the first bare word (if any) is a **project** substring; any bare
UUID-ish tokens are **session ids/prefixes**; `--async`, `--watch`, `--campaign <name>` pass
through. Map them onto the script's flags: `--project <p>`, `--session <id>` (repeatable),
`--async`, `--watch`, `--campaign <name>`.

Do the steps in order. **Never run `--deliver` before the user has seen the dry run and said yes.**

1. **Locate the backfill script.** Prefer `${CLAUDE_PLUGIN_ROOT}/scripts/backfill.py`. If that path
   does not exist, find the installed copy:

   ```bash
   ls "${CLAUDE_PLUGIN_ROOT}/scripts/backfill.py" 2>/dev/null \
     || ls ~/.claude/plugins/cache/*/codecollab/*/scripts/backfill.py 2>/dev/null | tail -1
   ```

2. **Dry run — show the breakdown so the user can decide.** Run the script with the user's
   `--project` / `--session` filters (and `--async` if they asked), but **without `--deliver`**:

   ```bash
   python3 <path-to-backfill.py>            # add: --project P  --session ID ...  --async   (as given)
   ```

   It prints a **projects → sessions → turns** breakdown (per-session detail appears once a
   `--project` or `--session` scope is given). **Show that breakdown to the user.** If they gave no
   scope, it's the whole history by project — offer to narrow to a project or specific sessions.

3. **Summarize and ask.** Report the totals in scope (N sessions / M turns). Tell the user:
   - It is **idempotent** — safe to re-run; the server dedups per turn and (in `--async`) a stable
     `--campaign` batch id + a local manifest let re-runs skip what's already uploaded.
   - Each turn triggers one distillation, so a large history is **real work** on the backend.
   - **Where it lands:** delivery goes to wherever `VONIC_BECOS_URL` points — **production by
     default**. For a local stack, the environment must be pointed there (`VONIC_BECOS_URL` at the
     local connector + a local token). Say which target this run will hit.

   Ask them to confirm the scope + target before delivering.

4. **Only after an explicit yes**, deliver:

   - **`--async` (recommended for large histories):** uploads each session once and returns; a
     server-side worker drains it turn-by-turn. Add `--watch` to poll status to completion, and
     `--campaign <name>` for a stable, resumable batch. Falls back to the sync drain if the
     connector doesn't advertise the backfill tools.
     ```bash
     python3 <path-to-backfill.py> --deliver --async  # + --project/--session/--watch/--campaign as chosen
     ```
   - **sync (default):** drives the whole drain from here, one turn at a time, retrying per session.
     ```bash
     python3 <path-to-backfill.py> --deliver          # + --project/--session as chosen
     ```

   Then report what landed. For `--async`, point the user at status: `becos_backfill_status` (or a
   re-run with `--watch`); jobs that fail carry a `last_error` and are safe to re-run. For sync,
   call out any sessions that were **deferred / timed out** (retryable — re-running is safe).

Delivery uses the same environment as live capture (`VONIC_BECOS_URL`, identity from the gateway
token or `VONIC_TENANT_ID` / `VONIC_USER_ID`, tokens). If the dry run shows sessions but delivery
fails with auth/connection errors, the capture environment isn't configured yet — point them at
setup rather than retrying.
