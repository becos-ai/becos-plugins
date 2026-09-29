---
description: Sign CodeCollab in through the browser, or complete a pending sign-in with its UUID
argument-hint: "[authorization-code-UUID]"
---

Authenticate the installed CodeCollab plugin without asking the user for an email, password, OTP,
token, or PKCE secret.

There is no Claude Code plugin API for a native-style split-screen dialog (that surface is a
built-in of the `claude` CLI itself, not something a plugin/command can render) — so this flow is
the closest approximation achievable from a markdown command: a clearly bordered, single-line URL
block, plus treating a bare UUID typed as your very next message (no need to re-run the slash
command) as completing the login.

1. Locate the installed script. Prefer `${CLAUDE_PLUGIN_ROOT}/scripts/connect.py`. If it does not
   exist, locate the newest installed copy:

   ```bash
   ls "${CLAUDE_PLUGIN_ROOT}/scripts/connect.py" 2>/dev/null \
      || ls ~/.claude/plugins/cache/*/codecollab/*/scripts/connect.py 2>/dev/null | sort -V | tail -1
   ```

2. If `$ARGUMENTS` is empty, start authorization:

   ```bash
   python3 <path-to-connect.py> start
   ```

   Parse the authorization URL out of the script's output (never invent or alter it) and present it
   like this, so the URL sits alone on its own line for a clean double/triple-click copy:

   ```
   ────────────────────────────────────────────────────────
   Open this URL to sign in to CodeCollab:

     <authorization_url>

   After you sign in, paste the UUID shown in the browser back here as your next message —
   no need to type /codecollab:login again.
   ────────────────────────────────────────────────────────
   ```

   Print no other commentary around it. Do not show anything from `~/.cache/codecollab`.

3. Treat EITHER of these as completing authorization: `$ARGUMENTS` contains exactly one UUID, OR
   (having just shown the URL block above in this session) the user's next message consists of
   exactly one UUID and nothing else. In either case run:

   ```bash
   python3 <path-to-connect.py> complete "<the-uuid>"
   ```

   Never inspect or display files under `~/.cache/codecollab`, and never print a token, verifier,
   or pending-state contents.

   - On success, reply with exactly the line `Codecollab login ok` (nothing else needs saying —
     the credential is stored and every subsequent turn continues normally, so there is no real
     "press enter to continue" gate to add beyond your normal reply).
   - On failure, report the safe error the script printed to stderr, and mention the URL can be
     re-requested with a bare `/codecollab:login`.

4. For any other argument (not empty, not a single UUID, and not a bare-UUID follow-up to a
   just-shown URL), do not run the script. Tell the user the accepted forms are
   `/codecollab:login` and `/codecollab:login <UUID>` (or pasting the UUID as your next message
   right after the URL is shown).
