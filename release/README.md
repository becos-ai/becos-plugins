# Releasing into this repository

This repo is **public**. The plugins are developed in private repos; only an explicit allowlist of
files is copied here, one squashed commit per release.

## The allowlist is the whole security model

[`payload.json`](payload.json) lists every path that may be published. It is an **allowlist, not an
ignore file** — a new file in a source repo stays private until someone adds it here deliberately.
An ignore file fails open; this fails closed.

It currently excludes tests, `__pycache__`, and `VENDORED_FROM.txt`. Nothing under a source repo's
`docs/`, and no `DECISIONS.md`, is listed at all.

## Root files are always public

Git's cone-mode sparse checkout never filters files at the repository root, and every consumer
clones the whole repo anyway. So the root may hold only publishable files. `validate.py` enforces
this against `allowed_root_files` / `allowed_root_dirs`.

## Cutting a release

Tag the **private** source repo. Its `publish payload` workflow does the rest:

```bash
git tag v0.23.3 && git push origin v0.23.3
```

The workflow copies the allowlisted payload here, runs `validate.py`, proves the copy matches the
source tree, commits as `release: <plugin> <version>`, and tags `<plugin>--v<version>`.

**Commit messages made here are public.** The workflow writes them for you; keep it that way.

## Doing it by hand

```bash
python3 release/sync_payload.py --plugin codecollab --from ~/vonic_code/becos-claude-plugin
python3 release/validate.py
python3 release/sync_payload.py --plugin codecollab --from ~/vonic_code/becos-claude-plugin --check
```

`--check` writes nothing and exits non-zero on any drift between this repo and the source tree.

## One-time setup

1. Create a fine-grained PAT scoped to `becos-ai/becos-plugins` with **Contents: read and write**.
2. Add it as secret `BECOS_PLUGINS_TOKEN` in each private source repo.
3. Copy [`workflows/publish-payload.yml`](workflows/publish-payload.yml) into each private repo at
   `.github/workflows/publish-payload.yml`, setting `PLUGIN_ID`:
   - `becos-ai/becos-claude-plugin` → `codecollab`
   - `becos-ai/codex-codecollab-plugin` → `codex-codecollab`

## Before this repo goes public

- [ ] Decide the licence. There is no `LICENSE` file yet, so everything here is "all rights
      reserved" by default. `becos-oc-plugin` ships Apache-2.0; the Codex manifest declares
      `Proprietary`. Those disagree — settle it before the first public push.
- [ ] Re-read `codecollab/` and `codex-codecollab/` in full. Publishing is irreversible: forks
      survive repository deletion.
