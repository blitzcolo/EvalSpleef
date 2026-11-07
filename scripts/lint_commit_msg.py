#!/usr/bin/env python3
"""Lint git commit messages against the EvalSpleef commit rules (see AGENTS.md).

Usage:
    lint_commit_msg.py <message-file>        # used by .githooks/commit-msg
    lint_commit_msg.py --message "text"      # lint a literal message
    lint_commit_msg.py --history [<range>]   # audit existing commits (default: all of HEAD)

Exit status is 0 when every message passes, 1 otherwise. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

CONVENTIONAL_TYPES = (
    "feat",
    "fix",
    "refactor",
    "perf",
    "test",
    "docs",
    "build",
    "ci",
    "chore",
    "style",
    "revert",
)

SUBJECT_RE = re.compile(
    r"^(?P<type>" + "|".join(CONVENTIONAL_TYPES) + r")"
    r"(?:\((?P<scope>[a-z0-9][a-z0-9._/-]*)\))?"
    r"(?P<breaking>!)?: (?P<desc>\S.*)$"
)
MAX_SUBJECT_LEN = 72


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    hint: str


def _rx(pattern: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(pattern, flags)


FORBIDDEN: tuple[Rule, ...] = (
    Rule("milestone", _rx(r"milestone|里程碑", re.I), "do not refer to milestones"),
    Rule(
        "phase-code",
        _rx(r"(?<![A-Za-z0-9])[MP]\d+(?:\.\d+)*(?![A-Za-z0-9])"),
        "do not use phase or priority codes such as M1, P0, P2.1",
    ),
    Rule(
        "work-package-code",
        _rx(r"(?<![A-Za-z0-9])W\d{2}(?![A-Za-z0-9])"),
        "do not use work-package codes such as W07",
    ),
    Rule(
        "stage-label",
        _rx(r"\b(?:phase|stage|wave|sprint|round)\s*[-:#]?\s*\d+\b", re.I),
        "do not label commits with a numbered phase, stage, wave, sprint or round",
    ),
    Rule(
        "co-author-trailer",
        _rx(r"\b(?:co-authored-by|generated-by|assisted-by)\s*:", re.I),
        "no Co-Authored-By, Generated-By or Assisted-By trailers",
    ),
    Rule(
        "generated-footer",
        _rx(r"generated (?:with|by)\b|\U0001f916", re.I),
        "no tool-generated footers",
    ),
    Rule(
        "session-link",
        _rx(
            r"session[\s_-]*(?:link|id|url)|\bsession\s*:|"
            r"https?://\S*(?:session|claude\.ai|chatgpt\.com|chat\.openai\.com|/share/|/s/)",
            re.I,
        ),
        "no session links or session identifiers",
    ),
    Rule(
        "agent-tooling",
        _rx(r"\b(?:ultrawork|mass-ulw|ulw|senpi|omo)\b", re.I),
        "do not mention the agent tooling used to produce the change",
    ),
)


def clean_message(raw: str) -> str:
    """Drop git comment lines and trailing whitespace the way git does."""
    lines = [ln.rstrip() for ln in raw.splitlines() if not ln.startswith("#")]
    return "\n".join(lines).strip("\n")


def lint(message: str) -> list[str]:
    msg = clean_message(message)
    problems: list[str] = []
    if not msg:
        return ["empty commit message"]

    subject = msg.splitlines()[0]
    is_git_generated = subject.startswith(("Merge ", 'Revert "'))
    if not is_git_generated:
        match = SUBJECT_RE.match(subject)
        if not match:
            problems.append(
                "subject must look like 'type(scope): imperative summary' with type in "
                + ", ".join(CONVENTIONAL_TYPES)
            )
        elif match.group("desc").endswith("."):
            problems.append("subject must not end with a period")
        elif match.group("desc")[0].isupper():
            problems.append("subject summary must start with a lowercase letter")
    if len(subject) > MAX_SUBJECT_LEN:
        problems.append(f"subject is {len(subject)} characters; keep it within {MAX_SUBJECT_LEN}")
    lines = msg.splitlines()
    if len(lines) > 1 and lines[1].strip():
        problems.append("leave a blank line between the subject and the body")

    for lineno, line in enumerate(lines, start=1):
        for rule in FORBIDDEN:
            hit = rule.pattern.search(line)
            if hit:
                problems.append(f"line {lineno}: {rule.hint} (matched {hit.group(0)!r})")
    return problems


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, timeout=60
    ).stdout


def audit_history(rev_range: str) -> int:
    sep_commit, sep_field = "\x01", "\x00"
    try:
        log = _git("log", "--format=%H%x00%an%x00%ae%x00%B%x01", rev_range)
    except subprocess.CalledProcessError as exc:
        print(f"git log failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    failures = 0
    checked = 0
    for chunk in log.split(sep_commit):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        sha, name, email, body = chunk.split(sep_field, 3)
        checked += 1
        problems = lint(body)
        identity = f"{name} <{email}>"
        if re.search(r"\b(claude|anthropic|openai|chatgpt|copilot|codex|gemini)\b", identity, re.I):
            problems.append(f"author identity looks like an AI agent: {identity}")
        if problems:
            failures += 1
            print(f"{sha[:10]} {body.splitlines()[0] if body else ''}")
            for problem in problems:
                print(f"    - {problem}")
    print(f"checked {checked} commit(s); {failures} failing")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("file", nargs="?", help="commit message file (as passed by git hooks)")
    parser.add_argument("--message", help="lint a literal message instead of a file")
    parser.add_argument(
        "--history",
        nargs="?",
        const="HEAD",
        metavar="RANGE",
        help="audit commits in RANGE (default HEAD)",
    )
    args = parser.parse_args(argv)

    if args.history is not None:
        return audit_history(args.history)
    if args.message is not None:
        text = args.message
    elif args.file:
        with open(args.file, encoding="utf-8") as handle:
            text = handle.read()
    else:
        parser.error("give a message file, --message, or --history")
    problems = lint(text)
    if problems:
        print("commit message rejected:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("See the 'Git and commits' section of AGENTS.md.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
