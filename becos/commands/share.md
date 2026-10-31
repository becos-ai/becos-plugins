---
description: Prepare a reviewed context and file handoff for one verified teammate
argument-hint: "@handle [message]"
---

Prepare a becos team handoff. An incidental @mention is never enough; this slash command is the
explicit sharing intent. The request is: `$ARGUMENTS`

1. Require one `@handle`. If absent, ask for it and stop. Verify it with `becos_handles`
   (`action: "resolve"`); if it does not resolve, say so and stop.
2. Pick the `kind` (bug, task, review, question, design or context) and build `objective`,
   `summary`, `rationale` and `package_text` as each field's description on the
   `becos_share` tool specifies, from explicit conversation content only, plus any message in
   the request. No hidden reasoning. Write for the recipient, who sees the sender's name separately,
   so never narrate the sender in the third person ("Ada asks you…"). Select at most 10 useful
   workspace files (25 MiB total) only when the request or conversation calls for them; never send
   every modified file. In a project without a git repository, files this session already persisted
   may be attached by reference (`persisted_file_ids` / `persisted_paths`). Every attachment gets a
   line under `## Files`; describe images and PDFs in words.
3. Optionally attach team evidence: `source_event_ids` may hold up to 50 citation ids that
   **actually appeared in this session** (a `<recalled-memory>` or `<entity-history>` block, or a
   resolve result) — never invented or altered — with `project_key` (this session's project).
4. Call `becos_share`. It records a local draft and sends nothing.
5. Show the returned recipient, kind, summary, rationale, files with sizes/hashes,
   `evidence_request` and draft id. Show every `completeness_issues` entry and every `notices` entry
   (images and PDFs are not scanned for secrets: ask the user to check them). Ask the user whether
   to send, and stop. With completeness issues, offer two choices: fix them (recommended: draft
   again with the missing sections), or send anyway.
6. Only on a later, unambiguous yes, call `becos_share_send` with the draft id. Pass
   `override_issues` (the issue codes) only if the user explicitly chose to send anyway. Claude Code then
   asks the user to approve the send itself; if they decline, nothing was sent. Report the sent
   handoff id, or the error verbatim. If a selected file changed since review, prepare a new draft.
   If evidence was requested and could not be frozen, nothing was sent: offer to retry, or to draft
   again without `source_event_ids`.

If the `becos_*` tools are not available, becos's tool server is not running in this
session; say so rather than working around it.
