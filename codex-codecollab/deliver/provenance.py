"""Repository-provenance instructions injected into every model turn by `recall.py`.

PROVENANCE_INSTRUCTIONS is a FAITHFUL copy of the "## Repository provenance in responses" section
of `docs/REPO_INSTRUCTIONS.md` in the `vonic_stack` repo (github.com/amitojch/vonic_stack), minus
that section's human-facing "> Enforcement note:" blockquote — which documents the mechanism for
readers and must not be fed to the model. The same body is mirrored in oc-codecollab-plugin's
`src/provenance.ts`; all three must stay byte-for-byte identical, because becos PARSES the citation
tokens this grammar produces. Keep them in sync by hand; `test_provenance.py` guards the copy here.

Why every turn: the tokens are not decoration. becos validates them verbatim against the exchange,
resolves the cited alias to `org/repo`, and re-partitions each derived fact by the repository it is
actually about rather than the session's single ambient cwd. A turn that states repository facts
without tokens is filed under the wrong repo, so the grammar has to be in front of the model on
every turn, not just the ones where memory happened to be recalled.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

PROVENANCE_INSTRUCTIONS = """\
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
# Cursor (`cur`) is the same case by another route: its plugin carries all three texts in an
# always-applied rule, and its `codecollab_recall` tool returns this hook's output as the digest.
_SELF_INJECTING_RUNTIMES = frozenset({"oc", "cur"})


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
RECALL_FEEDBACK_INSTRUCTIONS = """\
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
RESOLVE_TOOL_INSTRUCTIONS = """\
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
