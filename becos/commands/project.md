---
description: Choose or change which becos project this conversation is saved to (outside git repos)
argument-hint: "[project name | new [name]]"
---

Show the user which becos project this conversation is saved to, and change it if they ask.
A git repo's project is the repo itself and can't be changed. In other folders the project comes
from the user's `.becos-project` file; a change applies to this session only (the project is added
to the user's list in that file for next time).

The user's request (may be empty): `$ARGUMENTS`

1. Call `becos_project` and, if the request is empty, show its complete output as normal text
   (no question tool or multiple-choice widget). Tell the user to select one with
   `/becos:project <project name>`, or create one with `/becos:project new [name]`, then
   stop.
2. If the request names a project or starts with `new`, call `becos_project_use` with exactly
   that choice (`name`, or `new: true` with an optional name). Claude Code asks the user to approve
   re-filing the session; if they decline, nothing changed. A new name is lowercase letters, digits
   and dashes; if it is taken, the error suggests a free one: offer it.
3. Report the result in one line. Turns already saved stay in the project they were saved to. If a
   call fails, say projects are unavailable (re-run `/becos:login` if it mentions
   authentication).
