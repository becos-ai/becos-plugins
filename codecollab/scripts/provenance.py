"""CodeCollab's static instruction texts: repository provenance, recall feedback, resolve guidance.

Two sets, chosen by ``VONIC_CODECOLLAB_INSTRUCTIONS`` (see ``instructions_mode``):

- ``compact`` (default, 0.28.0): short texts, delivered ONCE per session and again after compaction
  by ``instructions.py session-start``, plus ``SESSION_REMINDER`` (one line) on every prompt from
  ``recall.py``. They were sent on every prompt before, and Claude Code keeps each hook's output in
  the transcript, so the same ~5.7k chars piled up turn after turn (60% of all hook context in a
  measured session).
- ``legacy``: the 0.27.3 texts and behaviour, byte for byte: the full texts on every prompt from
  ``recall.py`` and nothing at session start. This is the backup: set
  ``VONIC_CODECOLLAB_INSTRUCTIONS=legacy`` to revert without a release if citation or feedback
  quality drops. ``test_provenance.py`` pins the ``LEGACY_*`` texts by SHA-256 so they cannot drift.

The provenance text is a FAITHFUL copy of the provenance section of ``docs/REPO_INSTRUCTIONS.md``
(compact) and ``docs/REPO_INSTRUCTIONS_LEGACY.md`` (legacy) in the ``vonic_stack`` repo, minus the
human-facing "> Enforcement note:" blockquote, and is mirrored in becos-oc-plugin's
``src/provenance.ts``. Keep all three byte-for-byte identical: becos PARSES the citation tokens this
grammar produces (``provenance_citations.json`` is the frozen grammar). The feedback and resolve
texts are not mirrored contracts, but their machine-readable token forms must stay identical across
runtimes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


# The active (compact) set. becos resolves a cited alias by matching it against the LAST segment of
# the `org/repo` slug (becos_memforest coding/repo_resolver.py), so "without the owner" is load-bearing.
PROVENANCE_INSTRUCTIONS = """\
## Repository provenance
Tag each claim drawn from repository contents (a finding, decision, constraint, change, verification or reference) inline, in the message where you first state it, narration between tool calls included:
`[repo:<alias> path:<repo-relative-path> symbol:<symbol>]`
- `<alias>`: the repository's name from its `origin` remote, without the owner (not the local directory name; no `origin`: the directory name). `path:` and `symbol:` are optional; add them when known. Keys in this order; values contain no spaces or `]`.
- One repository per token. A sentence drawing on two repositories gets one token per clause.
- Name the repository you actually inspected; never guess. Uncertain: `[repo:unknown]`. Your own inference: `[analysis]`.
Example: `[repo:agent path:src/memory/store.py symbol:MemoryStore.save]`"""

# Worded as a standing rule (it is delivered once, not beside each digest), and it says a handoff
# is not recalled memory: the handoff quotes earlier replies' grade lines, which must not be graded.
RECALL_FEEDBACK_INSTRUCTIONS = """\
## Recalled-memory feedback
When a turn's context includes a `<recalled-memory>` block, end that reply with one line grading it; otherwise omit the line. A `<repository-handoff>` block is not recalled memory.
`[recall-relevance: <grade>] [recall-tokens-saved: ~<N>]` — <basis>
- `<grade>`: `low`, `medium`, `good`, `very good` or `excellent`: how relevant the recalled facts were to the turn.
- `<N>`: rough tokens the memory saved you (reads, searches, re-derivation); `~0` if none. `<basis>` names that work in a few words."""

# Only the judgement of WHEN to resolve. The server's own header on every recalled digest already
# names `vonic_resolve_event`, the `show_source.py` fallback and that it is a direct read; if that
# header ever stops naming them, they have to come back here.
RESOLVE_TOOL_INSTRUCTIONS = """\
## Expanding recalled memory
Resolve a recalled citation (`cite: fact_…` / `decision_…`) when it bears on the current step: before building on a recalled decision or constraint, repeating or rejecting a prior approach, or stating why something was done. Not for every id. A resolve shows what was recorded and why; current source shows what is true now, so check both when they matter."""

# One line on every prompt in compact mode, so the two machine-parsed outputs stay in view late in
# a long session; the full rules arrive at session start and after compaction.
# It must not contain the literal `<recalled-memory>` tag: the line rides EVERY prompt, and the
# feedback rule keys on that tag being present, so carrying it would make every turn look recalled.
SESSION_REMINDER = (
    "CodeCollab session rules apply: `[repo:…]` tokens on repository claims; "
    "a grade line after any turn that carries recalled memory."
)

# Entity history (entity_history.py): one paragraph, added only when that feature is on. Worded per
# runtime, because what delivers a file's history — and so how the model can ask for one — differs:
# Claude Code: a Read (or a one-time edit denial); Codex: only a one-time `apply_patch` denial
# (reads go through the shell); Cursor/Opencode: a read, or a `codecollab_recall` query naming the
# path. Opencode mirrors the `oc` text in src/provenance.ts (a test pins the copy).
_ENTITY_HISTORY_HEAD = """\
## File history
An `<entity-history>` block lists one file's recorded decisions, constraints, rejected approaches and superseded states, newest first, with `### symbol` sections for symbols in it. Treat it as recalled evidence: respect current constraints and don't repeat rejected approaches. """
ENTITY_HISTORY_INSTRUCTIONS = _ENTITY_HISTORY_HEAD + """\
It arrives once per file per session: with the first read of the file, or as a one-time denial of an edit to a file you have not read, in which case retry the same edit and it goes through. To see a file's history before deciding how to change it, read the file first. A prompt that names a repository-relative path or `path::Symbol` also brings that file's history into recalled memory."""
_ENTITY_HISTORY_CODEX = _ENTITY_HISTORY_HEAD + """\
It arrives once per file per session, as a one-time denial of the first `apply_patch` that updates or deletes a file whose history you have not been shown (at most three files per denial). Read it, adjust the patch if needed, and apply it again; it will not be denied again for those files. A prompt that names a repository-relative path or `path::Symbol` also brings that file's history into recalled memory."""
_ENTITY_HISTORY_TOOL = _ENTITY_HISTORY_HEAD + """\
It arrives once per file per session: with the first read of the file, or as a one-time denial of an edit to a file you have not read, in which case retry the same edit and it goes through. To get a file's or symbol's history on demand, call `codecollab_recall` with a `query` that names its exact repository-relative path or `path::Symbol` (e.g. `history of src/app/store.py::Store.save`); the result then carries a `History of …` section."""
_ENTITY_HISTORY_BY_TAG = {"cx": _ENTITY_HISTORY_CODEX, "cur": _ENTITY_HISTORY_TOOL,
                          "oc": _ENTITY_HISTORY_TOOL}


def entity_history_instructions(env: Mapping[str, str] | None = None) -> str:
    """The file-history paragraph for this runtime (``VONIC_CODECOLLAB_CLIENT_TAG``)."""
    source = os.environ if env is None else env
    tag = (source.get("VONIC_CODECOLLAB_CLIENT_TAG") or "cc").strip()
    return _ENTITY_HISTORY_BY_TAG.get(tag, ENTITY_HISTORY_INSTRUCTIONS)


# Team activity (team_activity.py -> vonic_team_activity): one paragraph telling the agent where
# "who did what" questions are answered. Claude Code runs the script (absolute path, since
# ${CLAUDE_PLUGIN_ROOT} is not set in the agent's own shell); Opencode and Cursor call their
# `codecollab_team` tool (Opencode mirrors the text in src/provenance.ts; a test pins the copy).
# Codex runs the script too, but its shell lacks the hook env, so the command sources
# ~/.codex/codecollab.env first — the wrapper its hooks.json and skills use.
_TEAM_ACTIVITY_HEAD = """\
## Team activity
For questions about what a person or the team did, decided or changed, or who is working on what \
(e.g. "what did nikhil do today?", "what changed in <repo> this week?", "who is working on \
<topic>?"), """
_TEAM_ACTIVITY_TAIL = """\
 It searches captured coding sessions across the team's repositories and returns facts and \
decisions with their author, repository and time. Answer from that result first and say so; git \
history is a secondary source (it misses discussions, decisions and uncommitted work) — label \
anything taken from it."""
_TEAM_ACTIVITY_TOOL = _TEAM_ACTIVITY_HEAD + (
    "call the `codecollab_team` tool with the user's question verbatim." + _TEAM_ACTIVITY_TAIL)


def _team_activity_cc() -> str:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team_activity.py")
    return _TEAM_ACTIVITY_HEAD + (
        f'run `python3 "{script}" -` with the Bash tool, giving the user\'s question verbatim on '
        "stdin (a quoted heredoc), never interpolated into the command line."
        + _TEAM_ACTIVITY_TAIL)


def _team_activity_cx() -> str:
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "team_activity.py")
    run = ("bash -lc 'set -a; [ -f \"$HOME/.codex/codecollab.env\" ] && "
           ". \"$HOME/.codex/codecollab.env\" >/dev/null 2>&1; set +a; "
           f"exec python3 \"{script}\" -'")
    return _TEAM_ACTIVITY_HEAD + (
        f"run `{run}` in the shell, giving the user's question verbatim on stdin (a quoted "
        "heredoc), never interpolated into the command line (the `codecollab-team` skill does "
        "this)." + _TEAM_ACTIVITY_TAIL)


def team_activity_instructions(env: Mapping[str, str] | None = None) -> str:
    """The team-activity paragraph for this runtime, or ``""`` where it has no entry point yet."""
    source = os.environ if env is None else env
    tag = (source.get("VONIC_CODECOLLAB_CLIENT_TAG") or "cc").strip()
    if tag == "cc":
        return _team_activity_cc()
    if tag == "cx":
        return _team_activity_cx()
    if tag in ("oc", "cur"):
        return _TEAM_ACTIVITY_TOOL
    return ""


def is_team_activity_enabled(env: Mapping[str, str] | None = None) -> bool:
    """On by default; ``VONIC_CODECOLLAB_TEAM_ACTIVITY=0`` drops the paragraph."""
    source = os.environ if env is None else env
    return (source.get("VONIC_CODECOLLAB_TEAM_ACTIVITY") or "1").strip() != "0"


_MODES = frozenset({"compact", "legacy"})


def instructions_mode(env: Mapping[str, str] | None = None) -> str:
    """``compact`` (default) or ``legacy``, from ``VONIC_CODECOLLAB_INSTRUCTIONS``. Anything else
    reads as the default, so a typo cannot silently drop the instructions altogether."""
    source = os.environ if env is None else env
    value = (source.get("VONIC_CODECOLLAB_INSTRUCTIONS") or "").strip().lower()
    return value if value in _MODES else "compact"


def instruction_texts(env: Mapping[str, str] | None = None) -> tuple[str, str, str]:
    """``(provenance, feedback, resolve)`` texts of the selected set."""
    if instructions_mode(env) == "legacy":
        return (LEGACY_PROVENANCE_INSTRUCTIONS, LEGACY_RECALL_FEEDBACK_INSTRUCTIONS,
                LEGACY_RESOLVE_TOOL_INSTRUCTIONS)
    return PROVENANCE_INSTRUCTIONS, RECALL_FEEDBACK_INSTRUCTIONS, RESOLVE_TOOL_INSTRUCTIONS


def session_instructions(env: Mapping[str, str] | None = None) -> str:
    """The text ``instructions.py session-start`` injects: the enabled compact sections, or nothing
    in legacy mode (legacy delivers on every prompt from ``recall.py`` instead)."""
    if instructions_mode(env) == "legacy":
        return ""
    prov, feedback, resolve = instruction_texts(env)
    parts = [text for text, on in ((prov, is_provenance_enabled(env)),
                                   (feedback, is_recall_feedback_enabled(env)),
                                   (resolve, is_resolve_tool_enabled(env)),
                                   (entity_history_instructions(env),
                                    is_entity_history_enabled(env)),
                                   (team_activity_instructions(env),
                                    is_team_activity_enabled(env)))
             if on and text]
    return "\n\n".join(parts)


def is_entity_history_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``VONIC_CODECOLLAB_ENTITY_HISTORY=1`` (off by default; see entity_history.py). Applies in
    both instruction modes: legacy has no session-start texts, so ``recall.py`` sends the paragraph
    with each prompt there (on runtimes where the hook is the instruction channel)."""
    source = os.environ if env is None else env
    return (source.get("VONIC_CODECOLLAB_ENTITY_HISTORY") or "").strip() == "1"


def session_reminder(env: Mapping[str, str] | None = None) -> str:
    """The per-prompt line in compact mode, when either parsed output is in play; else empty."""
    if instructions_mode(env) == "legacy":
        return ""
    if is_provenance_enabled(env) or is_recall_feedback_enabled(env):
        return SESSION_REMINDER
    return ""


# ---- legacy set (0.27.3), frozen: the revert path. Do not edit; test_provenance.py pins them. ----

LEGACY_PROVENANCE_INSTRUCTIONS = """\
## Repository provenance in responses

This workspace contains multiple Git repositories.

When answering questions based on repository contents, every substantive claim MUST identify the repository it was derived from, using a machine-readable citation token.

Repository aliases are the **git repository name** (from the `origin` remote), NOT the local
directory name. If a repository has no `origin` remote, use its directory / package name.

### Citation token format

Cite repository-derived claims with a bracketed, space-separated `key:value` token:

`[repo:<alias> path:<relative-file-path> symbol:<symbol>]`

* `repo:<alias>` is REQUIRED — the git repository name (see above). No spaces.
* `path:<relative-file-path>` is optional; include it whenever the claim is tied to a file. Repo-relative, no spaces.
* `symbol:<symbol>` is optional; include it when the claim is tied to a specific function, class, or method (e.g. `MemoryStore.save`). No spaces.

Keys appear in the order `repo` then `path` then `symbol`. Each token names exactly ONE repository. Values contain no spaces or `]`.

Examples:

`[repo:agent path:src/memory/store.py symbol:MemoryStore.save]`

`[repo:gateway path:app/routes/events.py]`

`[repo:agent]`

### Where to put tokens

* Cite in the SAME message where you first state a code-derived finding — this includes short narration between tool calls, not only the final answer or summary. A message that asserts a repository fact and omits its token is non-compliant even if a later message restates the claim with one. Do NOT defer citations to a wrap-up.
* Tag EACH durable claim inline — a decision, constraint, change, discovery, rejected approach, verification, or reference — with its own token, rather than citing once per message.
* If a single sentence draws on more than one repository, split it per clause so every clause carries a single-repo token. Do NOT list multiple repositories inside one token and do NOT use a comma-separated repo list.
* Use the token everywhere a repository is identified, including section headers (e.g. `### [repo:agent]`).

### Rules

* Do not omit the token from any claim derived from source code.
* Determine the repository from the path/reference actually inspected — never guess it from a filename or concept.
* Include `path:` (and `symbol:` when known) whenever the source location is known; prefer repo + path + symbol over the bare repo alias.
* For conclusions that are your own inference rather than facts found in a repository, use `[analysis]` instead of a repo token.
* If the repository is genuinely uncertain, use `[repo:unknown]` rather than guessing.
* Before sending ANY message that states a repository fact — not only long or final responses — perform a provenance pass and confirm every code-derived claim in that message carries a token."""


# Runtimes whose ADAPTER already injects these instructions itself, so the shared hook must not
# inject them a second time. Opencode's plugin pushes PROVENANCE_INSTRUCTIONS into the SYSTEM
# prompt (`experimental.chat.system.transform`) — a strictly better channel than ours, because the
# system prompt is replaced each turn instead of accumulating. It also consumes this hook's
# `additionalContext` verbatim as the recalled-memory digest (and renders it in the visible
# `codecollab_recall` tool), so prepending the grammar here would both duplicate it and pollute
# that digest. Claude Code and Codex have no such path: the hook is their only channel.
# Cursor (`cur`) is the same case by another route: its plugin returns all three texts from its
# `sessionStart` hook, and its `codecollab_recall` tool returns this hook's output as the digest.
_SELF_INJECTING_RUNTIMES = frozenset({"oc", "cur"})


def hook_injects_instructions(env: Mapping[str, str] | None = None) -> bool:
    """Whether this runtime's instruction texts come from the shared hooks (Claude Code, Codex) rather
    than its own adapter (see `_SELF_INJECTING_RUNTIMES`)."""
    source = os.environ if env is None else env
    return source.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc") not in _SELF_INJECTING_RUNTIMES


def is_provenance_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `recall.py` should inject the grammar on this turn.

    An explicit `VONIC_CODECOLLAB_PROVENANCE` always wins, in either direction ("0" disables, any
    other value enables). Unset, it defaults ON everywhere EXCEPT the runtimes that already inject
    it themselves (see `_SELF_INJECTING_RUNTIMES`), so a re-vendor cannot double-inject.
    """
    source = os.environ if env is None else env
    explicit = source.get("VONIC_CODECOLLAB_PROVENANCE")
    if explicit is not None:
        return explicit != "0"
    return source.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc") not in _SELF_INJECTING_RUNTIMES

# Recall-feedback instructions: a SEPARATE purpose from provenance, injected ONLY on turns where
# recall actually returned memory, so the model is asked to grade something genuinely present.
# Adapted from oc-codecollab-plugin's copy, which is explicitly NOT mirrored into
# vonic_stack/docs/REPO_INSTRUCTIONS.md (that file is provenance-only) and is therefore free to
# differ: the Opencode text also points at its `codecollab_recall` tool, which has no equivalent
# here — Claude Code and Codex receive recall solely as the `<recalled-memory>` block. The two
# machine-readable TOKEN forms are kept byte-identical so anything that later parses them works
# across all three runtimes. Display-only today: nothing captures these yet.
LEGACY_RECALL_FEEDBACK_INSTRUCTIONS = """\
## Recalled-memory feedback

The turn you are answering may be given recalled memory from earlier sessions, as a `<recalled-memory>` context block. Because that memory was provided this turn, end your response with a single recall-feedback line assessing it. If — and only if — no recalled memory was provided this turn (no `<recalled-memory>` block), omit this line entirely; never fabricate a grade for memory that was not recalled.

Emit the line as the very LAST line of your response, as two machine-readable tokens, distinct from any `[repo:…]` provenance tokens:

`[recall-relevance: <grade>] [recall-tokens-saved: ~<N>]` — <one-line basis>

1. `[recall-relevance: <grade>]` — how relevant the recalled facts were to what this turn actually needed. `<grade>` is exactly one of: `low`, `medium`, `good`, `very good`, `excellent`.
2. `[recall-tokens-saved: ~<N>]` — your estimate of the number of tokens the recalled memory saved this turn: work (file reads, greps, searches, re-derivation) you would otherwise have spent rediscovering those facts manually. `<N>` is a single rough integer. Follow the two tokens with a short one-line basis, e.g. "would've needed ~3 file reads + 2 greps". If the recalled memory was irrelevant and saved nothing, report `[recall-tokens-saved: ~0]`.

This estimate is a rough counterfactual, not an audited figure; keep it directional and let the basis line justify it."""


def is_recall_feedback_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `recall.py` should ask for a grade on a turn that DID recall memory.

    Same shape as `is_provenance_enabled`: `VONIC_CODECOLLAB_RECALL_FEEDBACK` wins in either
    direction, and unset it defaults ON except on runtimes that inject the text themselves.
    """
    source = os.environ if env is None else env
    explicit = source.get("VONIC_CODECOLLAB_RECALL_FEEDBACK")
    if explicit is not None:
        return explicit != "0"
    return source.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc") not in _SELF_INJECTING_RUNTIMES


# Resolve guidance: WHEN to expand a recalled citation, injected only on turns that actually
# recalled something (an id to expand is a precondition, exactly like the feedback grade).
#
# The recalled digest's own server-authored header already NAMES the resolver; this adds the
# judgement the header does not carry — which citations are worth the bundle and which are not,
# phrased conditionally so a model does not mechanically resolve everything it was given.
#
# Deliberately NOT placed inside the <recalled-memory> block: imperative text inside a recalled
# digest reads like injection and gets the whole digest discarded. Deliberately tolerant about
# HOW the citation is resolved, too — `vonic_resolve_event` is supplied by the configured MCP
# surface, not registered by this plugin, so an install without it still has the CLI path and the
# guidance stays true either way.
LEGACY_RESOLVE_TOOL_INSTRUCTIONS = """\
## Expanding recalled memory

Recalled memory contains compact facts, decisions, and constraints carrying cited ids (a `fact_id` or `decision_id`, shown as `cite: ...`). Expand a citation — with the `vonic_resolve_event` tool when your configured tools provide it, otherwise with `show_source.py <event-id>` — when doing so could help you understand or validate its evidence, history, rationale, provenance, or relationship to other decisions and facts.

A single resolve returns a bounded evidence bundle for that id: the canonical record, the linked decision or fact, the source turn's provenance (repository, branch, commit, time, and author), bounded observed content, changed files, one-hop lifecycle relations, and summarized tool activity. It is a direct read with no model call.

Resolving recalled evidence and inspecting the current source code are complementary. A resolve explains what was previously observed or decided and why; current source inspection establishes what the code does now. You may, and often should, do both when historical context and current behavior matter.

Resolve citations that could materially inform the current step — especially before implementing against a recalled decision, relying on a recalled constraint, repeating or rejecting a prior approach, or stating historical rationale. Prioritize the citations that matter rather than resolving every cited item mechanically."""


def is_resolve_tool_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether `recall.py` should explain when to expand a recalled citation.

    Same shape as the other two switches: `VONIC_CODECOLLAB_RESOLVE_TOOL` wins in either
    direction, and unset it defaults ON except on runtimes that inject the text themselves.
    """
    source = os.environ if env is None else env
    explicit = source.get("VONIC_CODECOLLAB_RESOLVE_TOOL")
    if explicit is not None:
        return explicit != "0"
    return source.get("VONIC_CODECOLLAB_CLIENT_TAG", "cc") not in _SELF_INJECTING_RUNTIMES
