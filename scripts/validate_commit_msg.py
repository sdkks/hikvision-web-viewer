"""Validate a Conventional Commit subject from a commit-msg hook file."""

import re
import sys
from pathlib import Path

SUBJECT = re.compile(
    r"(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([a-z0-9._-]*\))?!?: [^\s].*"
)


def valid_message(raw: str) -> bool:
    subject = next(
        (line for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")),
        "",
    )
    return bool(SUBJECT.fullmatch(subject)) and subject == subject.rstrip()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: validate_commit_msg.py <commit-msg-file>", file=sys.stderr)
        return 1
    try:
        raw = Path(args[0]).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("Cannot read commit message file as UTF-8.", file=sys.stderr)
        return 1
    if not valid_message(raw):
        print(
            "Expected <type>[(scope)][!]: <description>; e.g. feat: add output, "
            "feat(scope): add output, or feat(): add output. "
            "Types: build, chore, ci, docs, feat, fix, perf, refactor, revert, style, test.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
