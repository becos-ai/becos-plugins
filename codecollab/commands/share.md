---
description: Prepare a reviewed context and file handoff for one verified teammate
argument-hint: "@handle [message]"
---

Prepare a CodeCollab team handoff. An incidental @mention is never enough; this slash command is the
explicit sharing intent. The request is: `$ARGUMENTS`

1. Require one `@handle`. If absent, ask for it and stop. Resolve it through `command-json`, passing
   `{"command":"handles","action":"resolve","handle":"..."}` on stdin. Never interpolate the raw
   handle into a shell command or argument.
2. Build a concise package from explicit conversation content only: objective, summary, rationale,
   decisions and tradeoffs, rejected approaches, current state, open questions, and next steps. Do
   not include hidden reasoning. Select at most 10 useful workspace files, 25 MiB total. Never infer
   that all modified files should be sent.
3. Optionally attach team evidence. `source_event_ids` may contain up to 50 unique citation ids
   (`fact_…`, `decision_…`, event ids, page slugs) that **actually appeared in this session** — in a
   `<recalled-memory>` block, an `<entity-history>` block, or a resolve/`show_source` result. Never
   invent, guess, reconstruct or alter an id; omit the field when none appeared. `project_key` is
   required whenever ids are given (the project of this session). The server resolves those exact ids
   at confirmation and freezes the returned facts, decisions and citations into the immutable package;
   nothing is resolved locally and no model call is involved.
4. Create the reviewable draft by passing JSON on stdin. `files` contains workspace-relative paths;
   `source_refs` may contain explicit event/page citations:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/shared_handoff.py" command-json <<'CODECOLLAB_HANDOFF'
   {"command":"draft","payload":{"addressed_handle":"@handle","objective":"...","summary":"...","rationale":"...","package_text":"...","source_refs":[],"files":[],"project_key":"org/repo","source_event_ids":[]}}
   CODECOLLAB_HANDOFF
   ```

   In a project without a git repository, files this session already persisted may be attached **by
   reference** instead of re-uploaded: add `persisted_file_ids` (ids from `persist-turn`) and/or
   `persisted_paths` (the workspace-relative paths they were persisted under). They count against
   the same 10-file / 25 MiB budget and are listed in the draft review like selected files.
   Attaching is not sending.

5. Show the returned recipient, summary, rationale, selected files with sizes/hashes (persisted ones
   are marked `"persisted": true` and carry a `file_id`), the returned `evidence_request` (project
   key and the citation ids to be frozen), and the draft ID. Ask the user to confirm sending. Stop
   without calling `confirm`; only a later unambiguous affirmative response authorizes upload and
   send.

   This confirmation is a convention of this command, enforced by your compliance with it — Claude
   Code has no host permission prompt a plugin can require here, so do not describe it to the user as
   one, and do not call `confirm` on your own initiative or on an ambiguous reply.
6. On that later confirmation, pass `{"command":"confirm","handoff_id":"..."}` to `command-json`
   on stdin; never interpolate the raw ID into shell. Report the immutable sent handoff ID. If a
   selected path changed since review, stop and prepare a new draft. If confirmation fails, nothing
   was sent: report the error verbatim. When evidence was requested and the backend could not freeze
   it, the package is never sent without it — offer to retry the same confirmation, or to prepare a
   new draft without `source_event_ids`.
