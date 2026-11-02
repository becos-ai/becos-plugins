#!/usr/bin/env python3
"""Ask a team-activity question through `vonic_team_activity`.

    python3 team_activity.py <question...> [--person P] [--repo R] [--since ISO] [--until ISO]
    python3 team_activity.py - [...]        # the question is read from stdin (quote-safe)

Answers questions about what people on the team did, decided or changed, and who is working on
what ("what did nikhil do today?", "changes in becos-memforest this week", "who is working on
vonic-agent?"), from captured coding sessions across the tenant's repositories. The server plans
the question (one LLM call), validates it against the tenant's known people and repositories, and
filters deterministically; this script forwards the question and prints the result (a header
stating the filters used, then fact/decision lines with author badges, repo, time and cite ids),
with each cited entry marked [[n]][sn] and, at the end, the markdown link definitions for them.

Used by the `/becos-team` command and called by the agent itself (the session instructions
name this script's absolute path). Exit codes: 0 answered (or no matching activity), 1 the call
failed, 2 usage or misconfiguration. Env: the becos target/identity capture.py and recall.py use;
VONIC_TEAM_ACTIVITY_TIMEOUT (default 60s: planning + filtering takes longer than recall).
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — reuse becos config + identity resolution
import gbrain_client  # noqa: E402

TOOL = "vonic_team_activity"
_DEFAULT_TIMEOUT = 60.0
_CITED = re.compile(r"\(cite: ((?:decision|fact)_[0-9a-f]+)\)")
# A link this long still opens everywhere; a longer answer links its first ids only.
SOURCES_MAX_IDS = 50


def _timeout() -> float:
    try:
        value = float(os.environ.get("VONIC_TEAM_ACTIVITY_TIMEOUT", "") or _DEFAULT_TIMEOUT)
    except ValueError:
        return _DEFAULT_TIMEOUT
    return value if value > 0 else _DEFAULT_TIMEOUT


def _text(result: dict) -> str:
    return "\n".join(
        str(item.get("text", "")) for item in result.get("content", [])
        if isinstance(item, dict) and item.get("type") == "text"
    ).strip()


def arguments(question: str, *, person: str | None = None, repo: str | None = None,
              since: str | None = None, until: str | None = None) -> dict:
    """The tool arguments: the question verbatim plus any explicit filter that was given."""
    args = {"question": question.strip()}
    for key, value in (("person", person), ("repo", repo), ("since", since), ("until", until)):
        if value and value.strip():
            args[key] = value.strip()
    return args


def with_sources(text: str, becos_url: str) -> str:
    """``text`` with each cited entry marked ``[[n]][sn]`` (one number per id, in order) and a
    ready-made block of markdown link definitions at the end: ``[sn]: <site>/sources/<id>`` per
    entry and ``[s-all]: <site>/sources/<id>,<id>`` for all of them (the becos site's sources
    page; sign-in required, any team member). The agent copies an entry's marker after a claim and
    pastes the block as-is, so a rendered answer shows small clickable ``[n]``s, never ids or URLs,
    and no id is ever typed or invented. ``sn`` labels rather than ``n``: a label equal to the
    visible ``[n]`` makes some renderers (marked) nest a link inside the link. Unchanged when
    nothing is cited."""
    ids = list(dict.fromkeys(_CITED.findall(text)))
    if not ids:
        return text
    number = {cid: n for n, cid in enumerate(ids, 1)}
    text = _CITED.sub(lambda m: f"[[{number[m.group(1)]}]][s{number[m.group(1)]}] {m.group(0)}",
                      text)
    site = becos_url.split("/mcp", 1)[0].rstrip("/")
    linked = ids[:SOURCES_MAX_IDS]
    more = f" (the first {len(linked)} of {len(ids)})" if len(ids) > len(linked) else ""
    block = [f"[s{number[cid]}]: {site}/sources/{cid}" for cid in linked]
    block.append(f"[s-all]: {site}/sources/{','.join(linked)}")
    return (f"{text}\n\nSources{more}: after a claim, copy the marker of the entry it came from "
            f"(like [[1]][s1]); end the answer with \"Sources: [all {len(linked)}][s-all]\", a "
            "blank line, then these lines exactly as printed (they render as hidden links, "
            "sign-in required):\n" + "\n".join(block))


def ask(args: dict) -> str:
    url, token = capture._resolve_becos()
    result = gbrain_client.call_tool(
        url, token, TOOL, args, _timeout(), extra_headers=capture._becos_identity_headers()
    )
    return with_sources(_text(result), url)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="team_activity.py", description=TOOL)
    parser.add_argument("question", nargs="*")
    parser.add_argument("--person")
    parser.add_argument("--repo")
    parser.add_argument("--since")
    parser.add_argument("--until")
    ns = parser.parse_args(argv)
    if ns.question == ["-"]:
        question = sys.stdin.read().strip()
    else:
        question = " ".join(ns.question).strip()
    if not (question or ns.person or ns.repo):
        parser.print_usage(sys.stderr)
        sys.stderr.write("a question (or --person / --repo) is required\n")
        return 2
    try:
        text = ask(arguments(question, person=ns.person, repo=ns.repo, since=ns.since,
                             until=ns.until))
    except gbrain_client.GbrainError as exc:
        sys.stderr.write(f"{TOOL} failed: {exc}\n")
        return 1
    print(text or "No team activity matched.")
    return 1 if text.startswith("Error:") else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except gbrain_client.GbrainError as exc:  # misconfiguration (no url/token)
        sys.stderr.write(f"error: {exc}\n")
        sys.exit(2)
