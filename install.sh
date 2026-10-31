#!/usr/bin/env bash
# becos plugin installer for Claude Code, Codex and opencode.
#
#   curl -fsSL https://becos.ai/install.sh | bash                    # install + sign in
#   curl -fsSL https://becos.ai/install.sh | bash -s -- update       # update to the latest
#   curl -fsSL https://becos.ai/install.sh | bash -s -- uninstall    # remove becos
#   curl -fsSL https://becos.ai/install.sh | bash -s -- login        # sign in again
#
# Options: --only claude,codex,opencode   handle only these agents (default: every one installed)
#          --no-login                      skip the browser sign-in after install
#          --purge                         with uninstall: also delete the local sign-in and queue
#
# It finds which agents are on this machine and runs each one's own plugin commands. It also
# replaces the old codecollab plugin if it finds it. One sign-in covers every agent: they share
# ~/.cache/codecollab.
set -euo pipefail

MARKETPLACE_REPO="becos-ai/becos-plugins"
MARKETPLACE="becos"
OC_PACKAGE="@becos-ai/becos-opencode"
OC_OLD_PACKAGE="@becos-ai/oc-codecollab"
OC_FALLBACK_VERSION="0.35.0"

ACTION="install"
ONLY=""
LOGIN=1
PURGE=0

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; D=$'\033[2m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; D=""; G=""; Y=""; R=""; N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "$B" "$N" "$*"; }
ok()   { printf '  %s✔%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
becos plugin installer for Claude Code, Codex and opencode.

  curl -fsSL https://becos.ai/install.sh | bash                    install + sign in
  curl -fsSL https://becos.ai/install.sh | bash -s -- update       update to the latest
  curl -fsSL https://becos.ai/install.sh | bash -s -- uninstall    remove becos
  curl -fsSL https://becos.ai/install.sh | bash -s -- login        sign in again

Options: --only claude,codex,opencode   handle only these agents (default: every one installed)
         --no-login                      skip the browser sign-in after install
         --purge                         with uninstall: also delete the local sign-in and queue
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    install|update|uninstall|login) ACTION="$1" ;;
    --only) shift; ONLY="${1:-}" ;;
    --only=*) ONLY="${1#--only=}" ;;
    --no-login) LOGIN=0 ;;
    --purge) PURGE=1 ;;
    -h|--help|help) usage; exit 0 ;;
    *) fail "unknown argument: $1 (try: install, update, uninstall, login)" ;;
  esac
  shift
done

has() { command -v "$1" >/dev/null 2>&1; }

# Agents' own installers often add their folder to PATH only in ~/.zshrc or ~/.bashrc, which a
# `curl | bash` run may not have loaded. Look in those folders too.
for dir in "$HOME/.local/bin" "$HOME/.claude/local" "$HOME/.opencode/bin" "$HOME/.bun/bin" \
           "$HOME/.npm-global/bin" "/opt/homebrew/bin" "/usr/local/bin"; do
  if [ -d "$dir" ] && [[ ":$PATH:" != *":$dir:"* ]]; then PATH="$PATH:$dir"; fi
done
export PATH
wanted() { [ -z "$ONLY" ] || [[ ",$ONLY," == *",$1,"* ]]; }

# Prompts read from the terminal, not stdin: under `curl | bash` stdin is this script.
TTY=""
if [ -r /dev/tty ] && (exec </dev/tty) 2>/dev/null; then TTY=/dev/tty; fi
ask() {  # ask "question" -> reply in $REPLY; empty when there is no terminal
  REPLY=""
  [ -n "$TTY" ] || return 0
  printf '%s' "$1" >"$TTY"
  IFS= read -r REPLY <"$TTY" || REPLY=""
}

has python3 || fail "python3 is required (the becos plugins run on it). Install Python 3, then rerun."
py() { python3 -c "$@"; }

AGENTS=()
for a in claude codex opencode; do
  if wanted "$a" && has "$a"; then AGENTS+=("$a"); fi
done
if [ "$ACTION" != "login" ] && [ ${#AGENTS[@]} -eq 0 ]; then
  fail "no supported agent found on PATH (looked for: ${ONLY:-claude, codex, opencode})."
fi

# ---------- Claude Code ----------
claude_plugin_scope() {  # prints the scope plugin $1 is installed at, or nothing
  claude plugin list --json 2>/dev/null | py '
import json, sys
try: items = json.load(sys.stdin)
except Exception: items = []
for p in items:
    if p.get("id") == sys.argv[1]:
        print(p.get("scope") or "user"); break
' "$1"
}
claude_has_marketplace() {
  claude plugin marketplace list --json 2>/dev/null | py '
import json, sys
try: items = json.load(sys.stdin)
except Exception: items = []
sys.exit(0 if any(m.get("name") == sys.argv[1] for m in items) else 1)
' "$MARKETPLACE"
}
claude_install() {
  step "Claude Code"
  if claude_has_marketplace; then
    claude plugin marketplace update "$MARKETPLACE" >/dev/null && ok "marketplace updated"
  else
    claude plugin marketplace add "$MARKETPLACE_REPO" >/dev/null && ok "marketplace added"
  fi
  local old; old="$(claude_plugin_scope "codecollab@$MARKETPLACE")"
  if [ -n "$old" ]; then
    claude plugin uninstall "codecollab@$MARKETPLACE" --scope "$old" >/dev/null && ok "removed the old codecollab plugin"
  fi
  if [ -n "$(claude_plugin_scope "becos@$MARKETPLACE")" ]; then
    claude plugin update "becos@$MARKETPLACE" >/dev/null && ok "becos is up to date"
  else
    claude plugin install "becos@$MARKETPLACE" >/dev/null && ok "becos installed"
  fi
}
claude_uninstall() {
  step "Claude Code"
  local id scope removed=0
  for id in "becos@$MARKETPLACE" "codecollab@$MARKETPLACE"; do
    scope="$(claude_plugin_scope "$id")"
    if [ -n "$scope" ]; then
      claude plugin uninstall "$id" --scope "$scope" >/dev/null && ok "removed $id"; removed=1
    fi
  done
  if claude_has_marketplace; then
    claude plugin marketplace remove "$MARKETPLACE" >/dev/null && ok "removed the becos marketplace"
  fi
  [ "$removed" = 1 ] || ok "becos was not installed"
}

# ---------- Codex ----------
codex_installed() {  # exit 0 if plugin $1 is installed
  codex plugin list --json 2>/dev/null | py '
import json, sys
try: data = json.load(sys.stdin)
except Exception: data = {}
sys.exit(0 if any(p.get("pluginId") == sys.argv[1] and p.get("installed")
                  for p in data.get("installed", [])) else 1)
' "$1"
}
codex_has_marketplace() {
  codex plugin marketplace list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$MARKETPLACE"
}
codex_install() {
  step "Codex"
  if codex_has_marketplace; then
    codex plugin marketplace upgrade "$MARKETPLACE" >/dev/null && ok "marketplace updated"
  else
    codex plugin marketplace add "$MARKETPLACE_REPO" >/dev/null && ok "marketplace added"
  fi
  if codex_installed "codex-codecollab@$MARKETPLACE"; then
    codex plugin remove "codex-codecollab@$MARKETPLACE" >/dev/null && ok "removed the old codex-codecollab plugin"
  fi
  codex plugin add "becos@$MARKETPLACE" >/dev/null && ok "becos installed (latest)"
  CODEX_TOUCHED=1
}
codex_uninstall() {
  step "Codex"
  local id removed=0
  for id in "becos@$MARKETPLACE" "codex-codecollab@$MARKETPLACE"; do
    if codex_installed "$id"; then
      codex plugin remove "$id" >/dev/null && ok "removed $id"; removed=1
    fi
  done
  if codex_has_marketplace; then
    codex plugin marketplace remove "$MARKETPLACE" >/dev/null && ok "removed the becos marketplace"
  fi
  [ "$removed" = 1 ] || ok "becos was not installed"
}

# ---------- opencode ----------
oc_config_dir() { printf '%s' "${XDG_CONFIG_HOME:-$HOME/.config}/opencode"; }
oc_latest_version() {
  curl -fsSL "https://registry.npmjs.org/${OC_PACKAGE/\//%2f}/latest" 2>/dev/null \
    | py 'import json,sys; print(json.load(sys.stdin)["version"])' 2>/dev/null \
    || printf '%s' "$OC_FALLBACK_VERSION"
}
oc_remove_entries() {  # remove becos/codecollab entries from opencode's plugin lists; prints what it removed
  local pattern="$1"
  py '
import os, re, shutil, sys
directory, pattern = sys.argv[1], sys.argv[2]
entry = re.compile(r"\s*\"(?:" + pattern + r")(?:@[^\"]*)?\"(?:[ \t]*,)?")
changed = []
for name in ("opencode.jsonc", "opencode.json", "tui.json", "tui.jsonc"):
    path = os.path.join(directory, name)
    if not os.path.isfile(path):
        continue
    text = open(path, encoding="utf-8").read()
    new, count = entry.subn("", text)
    if not count:
        continue
    new = re.sub(r",(\s*\])", r"\1", new)  # no trailing comma before ]
    new = re.sub(r"\[\s*\]", "[]", new)
    shutil.copy2(path, path + ".becos-backup")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    changed.append(name)
if changed:
    print(", ".join(changed) + " (each backed up as <file>.becos-backup)")
' "$(oc_config_dir)" "$pattern"
}
oc_install() {
  step "opencode"
  local version; version="$(oc_latest_version)"
  local removed; removed="$(oc_remove_entries "$(printf '%s' "$OC_OLD_PACKAGE" | sed 's/[.]/\\./g')")"
  [ -z "$removed" ] || ok "removed the old oc-codecollab plugin from $removed"
  opencode plugin "$OC_PACKAGE@$version" -g -f >/dev/null 2>&1 \
    || fail "opencode could not install $OC_PACKAGE@$version"
  ok "becos $version installed"
}
oc_uninstall() {
  step "opencode"
  local removed; removed="$(oc_remove_entries "@becos-ai/(?:becos-opencode|oc-codecollab)")"
  if [ -n "$removed" ]; then ok "removed becos from $removed"; else ok "becos was not installed"; fi
}

# ---------- sign-in ----------
find_connect() {  # the plugin's own sign-in script, from whichever agent has it
  local f
  for f in \
    $(ls -d "$HOME"/.claude/plugins/cache/*/becos/*/scripts/connect.py 2>/dev/null | sort -V | tail -1) \
    $(ls -d "$HOME"/.codex/plugins/cache/*/becos/*/deliver/connect.py 2>/dev/null | sort -V | tail -1) \
    $(ls -d "${XDG_CACHE_HOME:-$HOME/.cache}"/opencode/packages/@becos-ai/becos-opencode@*/node_modules/@becos-ai/becos-opencode/deliver/connect.py 2>/dev/null | sort -V | tail -1); do
    if [ -f "$f" ]; then printf '%s' "$f"; return 0; fi
  done
  return 1
}
signed_in() { [ -f "${XDG_CACHE_HOME:-$HOME/.cache}/codecollab/gateway-auth.json" ]; }
open_url() {
  if has open; then open "$1" >/dev/null 2>&1 || true
  elif has xdg-open; then xdg-open "$1" >/dev/null 2>&1 || true
  fi
}
login() {
  step "Sign in"
  local connect
  if ! connect="$(find_connect)"; then
    warn "no becos plugin found to sign in with; install first, then rerun with: login"
    return 0
  fi
  if [ -z "$TTY" ]; then
    warn "no terminal to finish sign-in here; run /becos:login in Claude Code (or /becos-login in opencode)"
    return 0
  fi
  local out url
  out="$(python3 "$connect" start)" || { warn "could not start sign-in; try /becos:login in your agent"; return 0; }
  url="$(printf '%s\n' "$out" | grep -Eo 'https?://[^[:space:]]+' | head -1)"
  [ -n "$url" ] || { warn "sign-in did not return a link; try /becos:login in your agent"; return 0; }
  say "  Opening your browser to sign in. If it doesn't open, use this link:"
  say "    $url"
  open_url "$url"
  local code=""
  for _ in 1 2 3; do
    ask "  Paste the code the page shows, then press Enter: "
    code="$(printf '%s' "$REPLY" | tr -d '[:space:]')"
    [ -n "$code" ] || continue
    if python3 "$connect" complete "$code" >/dev/null; then
      ok "signed in"
      return 0
    fi
    warn "that code didn't work; check it and try again"
  done
  warn "not signed in; run this again with: login"
}

# ---------- run ----------
CODEX_TOUCHED=0
case "$ACTION" in
  install|update)
    for a in "${AGENTS[@]}"; do
      case "$a" in
        claude) claude_install ;;
        codex) codex_install ;;
        opencode) oc_install ;;
      esac
    done
    if [ "$LOGIN" = 1 ]; then
      if [ "$ACTION" = "install" ] && ! signed_in; then login
      elif [ "$ACTION" = "install" ]; then step "Sign in"; ok "already signed in (sign in again with: login)"
      fi
    fi
    step "Next"
    say "  Restart your agent so it loads becos."
    [ "$CODEX_TOUCHED" = 0 ] || say "  In Codex, run /hooks and trust the becos hooks, then start a new task."
    ;;
  uninstall)
    for a in "${AGENTS[@]}"; do
      case "$a" in
        claude) claude_uninstall ;;
        codex) codex_uninstall ;;
        opencode) oc_uninstall ;;
      esac
    done
    local_state="${XDG_CACHE_HOME:-$HOME/.cache}/codecollab"
    if [ "$PURGE" = 1 ]; then
      rm -rf "$local_state" && step "Local data" && ok "deleted your sign-in and unsent queue ($local_state)"
    else
      step "Local data"
      say "  Your sign-in and unsent queue are kept in $local_state."
      say "  To delete them too, rerun with: uninstall --purge"
    fi
    say ""
    say "  Restart your agent to finish."
    ;;
  login)
    login
    ;;
esac
