#!/usr/bin/env python3
"""Ask a team-activity question through `vonic_team_activity`.

    python3 team_activity.py <question...> [--person P] [--repo R] [--since ISO] [--until ISO]
    python3 team_activity.py - [...]        # the question is read from stdin (quote-safe)

Answers questions about what people on the team did, decided or changed, and who is working on
what ("what did nikhil do today?", "changes in becos-memforest this week", "who is working on
vonic-agent?"), from captured coding sessions across the tenant's repositories. The server plans
the question (one LLM call), validates it against the tenant's known people and repositories, and
filters deterministically; this script only forwards the question and prints the result: a header
stating the filters used, then fact/decision lines with author badges, repo, time and cite ids.

Used by the `/codecollab-team` command and called by the agent itself (the session instructions
name this script's absolute path). Exit codes: 0 answered (or no matching activity), 1 the call
failed, 2 usage or misconfiguration. Env: the becos target/identity capture.py and recall.py use;
VONIC_TEAM_ACTIVITY_TIMEOUT (default 60s: planning + filtering takes longer than recall).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402 — reuse becos config + identity resolution
import gbrain_client  # noqa: E402

TOOL = "vonic_team_activity"
_DEFAULT_TIMEOUT = 60.0


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


def ask(args: dict) -> str:
    url, token = capture._resolve_becos()
    result = gbrain_client.call_tool(
        url, token, TOOL, args, _timeout(), extra_headers=capture._becos_identity_headers()
    )
    return _text(result)


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
