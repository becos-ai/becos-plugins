#!/usr/bin/env python3
"""Copy a plugin's allowlisted payload from its private source repo into this public repo.

The allowlist lives in release/payload.json. Nothing outside it is ever copied, so a new
file in a source repo stays private until someone adds it here on purpose.

  sync_payload.py --plugin codecollab --from ~/vonic_code/becos-claude-plugin
  sync_payload.py --plugin codecollab --from ... --check   # exit 1 if public differs
"""
import argparse, fnmatch, json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = json.loads((ROOT / "release" / "payload.json").read_text())


def plugin(pid):
    for p in SPEC["plugins"]:
        if p["id"] == pid:
            return p
    sys.exit(f"unknown plugin '{pid}'; known: {[p['id'] for p in SPEC['plugins']]}")


def excluded(rel, patterns):
    s = str(rel)
    return any(fnmatch.fnmatch(s, pat) or fnmatch.fnmatch(s, pat.replace("**/", "")) for pat in patterns)


def collect(src, spec):
    """Every allowlisted file under src, as paths relative to the plugin root."""
    out = {}
    for entry in spec["include"]:
        base = src / entry
        if not base.exists():
            sys.exit(f"missing from source: {base}")
        for f in sorted(base.rglob("*") if base.is_dir() else [base]):
            if not f.is_file():
                continue
            rel = f.relative_to(src)
            if excluded(rel, spec["exclude"]):
                continue
            out[rel] = f
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--from", dest="src", required=True)
    ap.add_argument("--check", action="store_true", help="report drift, write nothing")
    a = ap.parse_args()

    spec = plugin(a.plugin)
    src = (Path(a.src).expanduser() / spec["source_subdir"]).resolve()
    dest = ROOT / spec["dest"]
    if not src.is_dir():
        sys.exit(f"source not a directory: {src}")

    files = collect(src, spec)
    if not files:
        sys.exit("allowlist matched nothing - refusing to publish an empty payload")

    version = json.loads((src / spec["manifest"]).read_text())["version"]

    if a.check:
        have = {p.relative_to(dest) for p in dest.rglob("*") if p.is_file()} if dest.exists() else set()
        drift = []
        drift += [f"only in public: {p}" for p in sorted(have - set(files))]
        drift += [f"only in source: {p}" for p in sorted(set(files) - have)]
        drift += [f"differs: {rel}" for rel, f in sorted(files.items())
                  if rel in have and f.read_bytes() != (dest / rel).read_bytes()]
        if drift:
            print("\n".join(drift))
            sys.exit(f"✘ {a.plugin}: {len(drift)} difference(s) vs source {version}")
        print(f"✔ {a.plugin} {version}: public payload matches source ({len(files)} files)")
        return

    if dest.exists():
        shutil.rmtree(dest)
    for rel, f in files.items():
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest / rel)
    print(f"✔ {a.plugin} {version}: published {len(files)} files -> {spec['dest']}/")
    print(f"::notice::{a.plugin} {version} ({len(files)} files)")


if __name__ == "__main__":
    main()
