---
description: Review the rules your team stated to its coding agents for this repository, and accept or reject them for yourself
argument-hint: "[accept 1, 3 | reject 2 | accept all | reject all | more]"
---

Show the user their becos rulebook for this repository and record their choices. Rules are
learned from the team's captured coding sessions (guardrails, coding conventions, project
decisions); each user accepts or rejects them for themselves. Accepted rules are given back to the
agent in every session.

The user's request (may be empty): `$ARGUMENTS`

1. List the rulebook (run it from the repository checkout):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook.py" list
   ```

   If that path does not exist, use the installed copy:
   `ls ~/.claude/plugins/cache/*/becos/*/scripts/rulebook.py 2>/dev/null | tail -1`.
   For "more", run `list --cursor <n>` with the cursor the previous output named.

2. Show the user the output: their accepted rules, then the numbered candidates, verbatim (keep
   the ids, who stated each rule, and the "restated since you rejected it" / "updated" notes).
   If the request above already says what to accept or reject, go to step 3; otherwise ask them to
   reply with e.g. "accept all", "reject all" or "accept 1, 3; reject 2", and stop.

3. Record exactly what the user chose, passing it on stdin (candidate numbers from the latest
   list, rule ids, or "all"; never guess choices the user did not make):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rulebook.py" decide <<'CODECOLLAB_RULEBOOK'
   {"accept": ["1", "3"], "reject": ["2"]}
   CODECOLLAB_RULEBOOK
   ```

4. Report what was recorded. The output ends with the user's "## Rulebook" section: those rules
   apply from now on in this session, and in their next sessions automatically. Choices are
   personal: they never change what teammates see. If a command fails, say the rulebook is
   unavailable (re-run `/becos:login` if it mentions authentication).
