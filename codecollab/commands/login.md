---
description: Sign CodeCollab in through the browser, or complete a pending sign-in with its UUID
argument-hint: "[authorization-code-UUID]"
---

Authenticate the installed CodeCollab plugin without asking the user for an email, password, OTP,
token, or PKCE secret.

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

   Show the command output to the user. It contains the browser URL and next step, but no secret.

3. If `$ARGUMENTS` contains exactly one UUID, complete authorization:

   ```bash
   python3 <path-to-connect.py> complete "$ARGUMENTS"
   ```

   Report success or the safe error printed by the script. Never inspect or display files under
   `~/.cache/codecollab`, and never print a token, verifier, or pending-state contents.

4. For any other argument, do not run the script. Tell the user the accepted forms are
   `/codecollab:login` and `/codecollab:login <UUID>`.
