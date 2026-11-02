---
description: Ask what the team did, decided or changed, or who is working on what (from captured coding sessions)
argument-hint: "<question>  e.g. what did nikhil do today? | changes in becos-memforest this week | who is working on vonic-agent?"
---

Answer the user's team-activity question from becos's captured coding sessions.

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
   ls ~/.claude/plugins/cache/*/becos/*/scripts/team_activity.py 2>/dev/null | tail -1
   ```

3. Answer **only** from its output, for a reader who leads the team: themes, not a log. Do not
   add items from git, files or memory unless the user asks; if you do, label them as coming from
   git.
   - **Scope first:** one line repeating the output's first line (people, repositories, time
     window). A day in the window with no entries means no work was recorded that day: say which
     days, and when the latest entry was.
   - **A week or longer:** what shipped, then one line per theme (infra, tooling, a feature…),
     each saying what changed for users rather than which file or setting changed. Then **now**:
     what the latest sessions show; when they don't show it, say it's unclear and why.
   - **One day:** lead with the day's main outcome, then risks on their own line, then smaller
     items, then what was left open.
   - **Plain language:** keep cite ids, file paths, flags and commit hashes out of the sentences.
     Write people's names, not emails, when the output makes the person clear.
   - **Numbered sources:** each entry in the output carries a ready-made link like
     `[[1]](https://…/sources/…)`; copy it right after a claim taken from that entry. Readers see a
     clickable `[1]`. Never write an id or a URL of your own.
   - **Who said it:** an entry marked as proposed by an agent was the agent's, not the person's;
     say so and, when the output shows it, what it was answering.
   - **Sources at the end:** finish with the output's closing `Sources: [all N](…)` line exactly
     as printed. Never build or edit a link.
     If the output has no numbered entries, list the cite ids once at the end instead.
   - **When another agent will read the answer** (the user asks for a handoff, a file or
     structured data): give fields instead of prose — scope, latest entry, days with no entries,
     now (or `unknown`), recently finished and risks, each item with its cite id.
4. If the output says nothing matched, say so and suggest widening the window or naming a
   person/repository. If it starts with `Error:` or the script fails, report that recall is
   unavailable (re-run `/becos:login` if it mentions authentication).
