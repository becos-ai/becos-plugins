---
description: Choose or change which CodeCollab project this conversation is saved to (outside git repos)
argument-hint: "[project name | new [name]]"
---

Show the user which CodeCollab project this conversation is saved to, and change it if they ask.
A git repo's project is the repo itself and can't be changed. In other folders the project comes
from the user's `.becos-project` file; a change applies to this session only (the project is added
to the user's list in that file for next time).

The user's request (may be empty): `$ARGUMENTS`

Use the `--cwd` and `--session` values from the "CodeCollab project" line in your session context
(the session's folder and id). If that line is not in your context, use the current directory and
omit `--session` for `list`.

1. List the choices:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/project.py" --cwd "<cwd>" --session "<session id>" list
   ```

   If that path does not exist, use the installed copy:
   `ls ~/.claude/plugins/cache/*/codecollab/*/scripts/project.py 2>/dev/null | tail -1`.

2. If the request is empty, respond with the complete output of the `list` command as normal text.
   Do not use a question tool or a multiple-choice widget. Tell the user to select a project with
   `/codecollab:project <project name>`, or create one with `/codecollab:project new [name]`, then
   stop and wait for their next command.

3. If the request already names a project or starts with `new`, record exactly that choice:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/project.py" --cwd "<cwd>" --session "<session id>" use "<name>"
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/project.py" --cwd "<cwd>" --session "<session id>" use --new ["<name>"]
   ```

   A new project name is lowercase letters, digits and dashes; without a name one is generated.
   If the name is taken, the error suggests a free one: offer it to the user.

4. Report the result in one line. Turns already saved stay in the project they were saved to. If
   a command fails, say projects are unavailable (re-run `/codecollab:login` if it mentions
   authentication).
