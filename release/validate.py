#!/usr/bin/env python3
"""Guard the public repo. Run before every push; CI runs it on every commit and PR.

Checks, in order of how badly each one bites:
  1. root hygiene      - root files land in EVERY consumer clone and sparse-checkout
                         never filters them, so the root must hold only publishable files
  2. no private files  - design docs, tests, caches, vendoring provenance
  3. manifests resolve - each marketplace source path exists and has its plugin manifest
  4. versions agree    - plugin.json vs the version string on the plugin card
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = json.loads((ROOT / "release" / "payload.json").read_text())
FORBIDDEN = ["DECISIONS.md", "PLAN.md", "SPIKE.md", "AGENTS.md", "VENDORED_FROM.txt",
             "RELEASING.md", "SERVER_SETUP.md", "*-design.md", "test_*.py", "*.pyc"]
fail = []


def check(ok, msg):
    print(f"  {'✔' if ok else '✘'} {msg}")
    if not ok:
        fail.append(msg)


print("root hygiene")
files = {p.name for p in ROOT.iterdir() if p.is_file()}
dirs = {p.name for p in ROOT.iterdir() if p.is_dir() and p.name != ".git"}
stray_f = sorted(files - set(SPEC["allowed_root_files"]))
stray_d = sorted(dirs - set(SPEC["allowed_root_dirs"]))
check(not stray_f, f"no unlisted files at repo root{': ' + ', '.join(stray_f) if stray_f else ''}")
check(not stray_d, f"no unlisted dirs at repo root{': ' + ', '.join(stray_d) if stray_d else ''}")

print("no private files anywhere")
tracked = [p for p in ROOT.rglob("*") if p.is_file() and ".git/" not in str(p)]
for pat in FORBIDDEN:
    hits = [str(p.relative_to(ROOT)) for p in tracked if p.match(pat)]
    check(not hits, f"no {pat}{': ' + ', '.join(hits[:3]) if hits else ''}")

print("manifests resolve")
cc = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
cx = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
sources = [(e["name"], e["source"]) for e in cc["plugins"]]
sources += [(e["name"], e["source"]["path"]) for e in cx["plugins"]]
for name, src in sources:
    check((ROOT / src).is_dir(), f"{name}: source {src} exists")

print("versions agree")
for spec in SPEC["plugins"]:
    mf = ROOT / spec["dest"] / spec["manifest"]
    if not mf.exists():
        check(False, f"{spec['id']}: {spec['manifest']} missing")
        continue
    data = json.loads(mf.read_text())
    version = data["version"]
    check(bool(version), f"{spec['id']}: version {version}")
    # the plugin card repeats the version in prose; a stale one ships a lie to users
    for field, text in (data.get("interface") or {}).items():
        if isinstance(text, str):
            for found in re.findall(r"\bv(\d+\.\d+\.\d+)\b", text):
                check(found == version,
                      f"{spec['id']}: interface.{field} says v{found}, manifest says {version}")

print()
if fail:
    sys.exit(f"✘ {len(fail)} check(s) failed")
print("✔ all checks passed")
