---
description: Ask what the team did, decided or changed, or who is working on what (from captured coding sessions)
argument-hint: "<question>  e.g. what did nikhil do today? | changes in becos-memforest this week | who is working on vonic-agent?"
---

Answer the user's team-activity question from CodeCollab's captured coding sessions.

The question is: `$ARGUMENTS`

1. If the question is empty, ask the user what they want to know (a person, a repository, a
   topic, or a time range) and stop.
2. Run the team-activity script, passing the question **verbatim on stdin** (never interpolate it
   into the command line — quotes or `$(...)` in a question must stay text):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/team_activity.py" - <<'CODECOLLAB_QUESTION'
   <the question, verbatim>
   CODECOLLAB_QUESTION
   ```

   If that path does not exist, use the installed copy:

   ```bash
   ls ~/.claude/plugins/cache/*/codecollab/*/scripts/team_activity.py 2>/dev/null | tail -1
   ```

3. Answer **only** from its output. The first line states the filters it used (people,
   repositories, time window, topic) — repeat that scope in one short line, then summarise the
   activity: group by person or day as the output does, keep the author emails and repositories,
   and cite ids where a claim rests on one entry. Do not add items from git, files or memory
   unless the user asks; if you do, label them as coming from git.
4. If the output says nothing matched, say so and suggest widening the window or naming a
   person/repository. If it starts with `Error:` or the script fails, report that recall is
   unavailable (re-run `/codecollab-login` if it mentions authentication).
