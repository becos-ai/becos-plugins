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

## One-time setup — done

1. ✅ A fine-grained PAT scoped to `becos-ai/becos-plugins` with **Contents: read and write**.
2. ✅ Added as secret `BECOS_PLUGINS_TOKEN` in each private source repo.
3. ✅ [`workflows/publish-payload.yml`](workflows/publish-payload.yml) installed in each private repo
   at `.github/workflows/publish-payload.yml`, with `PLUGIN_ID` set:
   - `becos-ai/becos-claude-plugin` → `codecollab`
   - `becos-ai/codex-codecollab-plugin` → `codex-codecollab`

Verified live on 2026-09-22: both repos dispatched the workflow and it ran green end to end —
checkout, allowlist copy, `validate.py`, and the `--check` drift proof — then correctly published
nothing, because the payloads already matched (`No payload change for codecollab 0.23.2`). The
publish-and-tag branch of the job has not fired yet; the next real release exercises it.

### Why the credential exists

A workflow's automatic `GITHUB_TOKEN` only works on the repo the workflow lives in. Publishing writes
to a *different* repo, so `actions/checkout` needs a credential of its own. That is the only place
`BECOS_PLUGINS_TOKEN` is used.

### Alternative to consider: a deploy key

A **deploy key** is an SSH keypair attached to `becos-plugins` itself rather than to a person. It is
the more correct choice; the PAT is the pragmatic one we started with.

| | fine-grained PAT (current) | deploy key |
| --- | --- | --- |
| Tied to | a user account | the repo |
| Survives that user leaving the org | ✘ | ✔ |
| Can be widened later | ✔ (someone edits its scope) | ✘ (one repo, by construction) |
| Expiry | set at creation; silently breaks CI when it lapses | none |
| Setup | generate in the UI, paste into two repos | generate a keypair, add the public half to
  `becos-plugins` with write access, the private half as a secret in each source repo, and switch
  `actions/checkout` to `ssh-key:` |

**Switch when** either becomes true: the PAT's expiry is near, or this pipeline needs to outlive the
account that owns the token. Two source repos would need a workflow change, plus one line in the
public repo's deploy-key settings.

**Not** a reason to switch: leak risk alone. The fine-grained PAT can write to exactly one repo whose
contents are meant to be public, so the blast radius is already small.

## Before this repo goes public

- [x] Decide the licence. **Apache-2.0**, matching `becos-oc-plugin` — the `LICENSE` file here is
      byte-identical to its. That plugin is already published to public npm under Apache-2.0, and its
      tarball ships `deliver/`, so the shared deliverer is permissively licensed in the wild already;
      anything stricter here would only be inconsistent, not protective.
- [ ] Two follow-ups this exposes, both needing an owner's call:
      - The Codex manifest declares `"license": "Proprietary"`, which now contradicts this repo and
        the npm package built from the same vendored code.
      - The Claude manifest declares no licence at all.
      - Neither this `LICENSE` nor the oc one fills in the appendix's
        `Copyright [yyyy] [name of copyright owner]`, so no copyright holder is asserted anywhere.
- [ ] Re-read `codecollab/` and `codex-codecollab/` in full. Publishing is irreversible: forks
      survive repository deletion.
