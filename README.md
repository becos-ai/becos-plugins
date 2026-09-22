# Becos codecollab plugins

Team-scoped, evidence-first memory for AI coding agents. codecollab captures each turn of your
coding sessions to your team's becos brain, and recalls the relevant parts back before your next
prompt — so context survives across sessions, machines, and teammates.

**Never sent:** your thinking, tool calls, code, or patches. Prompts and text replies only, unless
you explicitly opt in.

This repository is a plugin marketplace for **Claude Code** and **Codex**. The opencode plugin
ships on npm (see below).

## Claude Code

```bash
claude plugin marketplace add becos-ai/becos-plugins
claude plugin install codecollab@becos
```

Restart Claude Code, then:

```
/codecollab:login
```

`/codecollab:backfill` imports your existing sessions.

## Codex

```bash
codex plugin marketplace add becos-ai/becos-plugins
codex plugin add codex-codecollab@becos
```

Restart Codex, run `/hooks` to trust the plugin's hooks, start a **fresh** task, and type:

```
log in
```

There is no slash command for login in Codex — the plugin recognises the phrase.

## opencode

Pin an exact version in `~/.config/opencode/opencode.jsonc`; opencode installs it on next launch
and caches by version, so bumping the pin is the upgrade:

```jsonc
{ "plugin": ["@becos-ai/oc-codecollab@0.12.1"] }
```

Then fully quit (Cmd-Q) and relaunch opencode.

## Updating

Every release bumps the plugin version — nothing refreshes otherwise, because each runtime runs a
per-version *cache copy* rather than this repository.

```bash
claude plugin marketplace update becos && claude plugin update codecollab@becos
codex plugin marketplace upgrade becos
```

For Codex, re-trust the hooks with `/hooks` and start a fresh task: plugins are snapshotted when a
task begins, so a task opened before the upgrade keeps the old copy.

## What's in this repository

| Path | What it is |
| --- | --- |
| `codecollab/` | the Claude Code plugin |
| `codex-codecollab/` | the Codex plugin |
| `.claude-plugin/`, `.agents/` | the two marketplace manifests |
| `release/` | how payloads get published here |

This is a **release repository**. Its contents are published from the plugins' development repos,
one squashed commit per release. Issues and questions are welcome here.
