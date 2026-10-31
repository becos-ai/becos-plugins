# becos plugins

Team-scoped, evidence-first memory for AI coding agents. becos captures each turn of your coding
sessions to your team's becos brain, and recalls the relevant parts back before your next prompt —
so context survives across sessions, machines, and teammates.

**What is sent.** In Claude Code, capture is on by default for the prompt and text reply, the
commands run, their output, code edits and the paths of files edited, plus the repository, branch,
author and commit. Bodies are secret-redacted (best effort, not a guarantee) and size-capped, and
each category can be turned off (`VONIC_CAPTURE_COMMANDS`/`OUTPUTS`/`CODE`/`FILES=0`). Your
thinking is never sent unless you opt in (`VONIC_CAPTURE_THINKING=1`, off by default). The Codex
plugin sends prompts, text replies, code blocks from replies and edited file paths; see its plugin
card for details.

This repository is a plugin marketplace for **Claude Code** and **Codex**. The opencode plugin
ships on npm (see below).

## Install, update or remove with one command

```bash
curl -fsSL https://becos.ai/install.sh | bash
```

[`install.sh`](install.sh) finds which of Claude Code, Codex and opencode are on your machine,
installs becos in each (replacing the old codecollab plugin if it finds it), and signs you in once
in the browser; one sign-in covers every agent. Then restart your agent. In Codex, also run `/hooks`
and trust the becos hooks.

```bash
curl -fsSL https://becos.ai/install.sh | bash -s -- update      # update to the latest
curl -fsSL https://becos.ai/install.sh | bash -s -- uninstall   # remove becos (add --purge to delete the local sign-in too)
curl -fsSL https://becos.ai/install.sh | bash -s -- login       # sign in again
```

`--only claude,codex,opencode` limits it to some agents; `--no-login` skips the sign-in. It needs
`python3`, which the plugins run on. becos.ai/install.sh fetches the script from this repository,
so you can read exactly what runs. To run the steps yourself instead, use the sections below.

## Claude Code

```bash
claude plugin marketplace add becos-ai/becos-plugins
claude plugin install becos@becos
```

Restart Claude Code, then:

```
/becos:login
```

`/becos:backfill` imports your existing sessions.

## Codex

```bash
codex plugin marketplace add becos-ai/becos-plugins
codex plugin add becos@becos
```

Restart Codex, run `/hooks` to trust the plugin's hooks, start a **fresh** task, and type:

```
log in
```

There is no slash command for login in Codex — the plugin's `becos-login` skill recognises the
phrase.

## opencode

Pin an exact version in `~/.config/opencode/opencode.jsonc`; opencode installs it on next launch
and caches by version, so bumping the pin is the upgrade:

```jsonc
{ "plugin": ["@becos-ai/becos-opencode@0.35.0"] }
```

Then fully quit (Cmd-Q) and relaunch opencode.

## Updating

Every release bumps the plugin version — nothing refreshes otherwise, because each runtime runs a
per-version *cache copy* rather than this repository.

```bash
claude plugin marketplace update becos && claude plugin update becos@becos
codex plugin marketplace upgrade becos
```

For Codex, re-trust the hooks with `/hooks` and start a fresh task: plugins are snapshotted when a
task begins, so a task opened before the upgrade keeps the old copy.

## Upgrading from codecollab

codecollab is now becos. The old plugins (`codecollab@becos` for Claude Code,
`codex-codecollab@becos` for Codex, `@becos-ai/oc-codecollab` on npm) are now retired stubs: they
capture nothing, recall nothing and have no tools, and only tell you how to switch. Your sign-in,
pending turns and history carry over — local state and every `VONIC_*` setting keep their names, so
you do not sign in again. Having both installed never captures twice.

The install script above makes this switch for you in every agent it finds. To do it by hand:

**Claude Code** — swap the plugin, then restart Claude Code:

```bash
claude plugin marketplace update becos
claude plugin uninstall codecollab@becos     # add --scope project if you installed it per project
claude plugin install becos@becos            # same scope as the old install
```

Commands are now `/becos:login`, `/becos:backfill`, `/becos:project`, `/becos:rulebook`,
`/becos:share`, `/becos:shared` and `/becos:team`. If a permission rule in your `settings.json` names
`mcp__plugin_codecollab_codecollab__…` or a `codecollab_*` tool, rename it to
`mcp__plugin_becos_becos__…` / `becos_*`.

**Codex** — swap the plugin:

```bash
codex plugin marketplace upgrade becos
codex plugin remove codex-codecollab@becos
codex plugin add becos@becos
```

Then restart Codex, re-trust the hooks in `/hooks` (they are new hooks under a new plugin id) and
start a fresh task. Skills are now `becos-login` … `becos-shared` and tools `becos_*`; rename any
rule or setting that named `codecollab_*` tools or `codex-codecollab@becos`.

**opencode** — change the pin from `@becos-ai/oc-codecollab@…` to
`@becos-ai/becos-opencode@0.35.0`, then quit and relaunch opencode.

Gateway tools (`vonic_*`, e.g. `vonic_resolve_event`) keep their names everywhere.

## What's in this repository

| Path | What it is |
| --- | --- |
| `becos/` | the Claude Code plugin (`becos@becos`) |
| `becos-codex/` | the Codex plugin (`becos@becos` in the Codex marketplace) |
| `codecollab/`, `codex-codecollab/` | the retired codecollab stubs that point users to becos |
| `.claude-plugin/`, `.agents/` | the two marketplace manifests |
| `install.sh` | the one-command installer that becos.ai/install.sh runs |
| `release/` | how payloads get published here |

Licensed under [Apache-2.0](LICENSE).

This is a **release repository**. Its contents are published from the plugins' development repos,
one squashed commit per release. Issues and questions are welcome here.
