#!/usr/bin/env python3
"""Fail when tracked files contain credential-looking values.

This is a guard, not a proof. The rules only cover credential formats this
repository has actually leaked or that are unambiguous in the wild, so a new
provider's token format can still pass. Its job is to stop the same classes of
leak from being committed again.

The scanner deliberately never prints the matched value -- only
``path:line: rule`` -- so running it in CI or a pre-commit hook cannot copy a
secret into build logs.

Usage::

    python scripts/check_secrets.py              # every tracked file
    python scripts/check_secrets.py --staged     # files staged for commit
    python scripts/check_secrets.py FILE ...     # explicit files
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Patterns are assembled from fragments so that this file does not match
# itself when the tracker scans it.
_RULES: tuple[tuple[str, str], ...] = (
    ("context7", "ctx7" + r"sk-[A-Za-z0-9_-]{10,}"),
    ("tavily", "tv" + r"ly-[A-Za-z0-9_-]{10,}"),
    ("firecrawl", "fc" + r"-[0-9a-f]{20,}"),
    ("openai", "sk" + r"-[A-Za-z0-9]{20,}"),
    ("github", "gh" + r"[pousr]_[A-Za-z0-9]{20,}"),
    ("aws-access-key", "AK" + r"IA[0-9A-Z]{16}"),
)
_COMPILED = tuple((name, re.compile(pattern)) for name, pattern in _RULES)

# Spans that are unambiguously placeholders rather than credentials. They are
# removed before testing so a line may hold both a placeholder and a real
# value and the real value is still caught.
_PLACEHOLDER = re.compile(
    r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"  # ${VAR}
    r"|\$\{env:[^}]+\}"  # ${env:VAR}
    r"|<<[^>\n]*>>"  # <<REPLACE_ME>>
    r"|<REDACTED[^>\n]*>"  # <REDACTED>
)


def find_secrets(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, rule_name)`` for every credential-looking line."""
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = _PLACEHOLDER.sub("", line)
        for name, pattern in _COMPILED:
            if pattern.search(stripped):
                hits.append((lineno, name))
    return hits


def tracked_files() -> list[Path]:
    """Files git knows about, resolved against the repository root."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    names = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return [REPO_ROOT / name for name in names if name]


def staged_files() -> list[Path]:
    """Files added or modified in the index (what the next commit contains)."""
    completed = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    names = completed.stdout.decode("utf-8", errors="replace").split("\0")
    return [REPO_ROOT / name for name in names if name]


def scan(paths: list[Path]) -> list[tuple[str, int, str]]:
    """Scan ``paths`` and return ``(relative_path, line, rule)`` findings."""
    findings: list[tuple[str, int, str]] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        # Skip binaries: a NUL byte in the first block is the usual heuristic.
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for lineno, rule in find_secrets(text):
            try:
                shown = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                shown = str(path)
            findings.append((shown, lineno, rule))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--staged",
        action="store_true",
        help="scan only files staged for commit (pre-commit hook mode)",
    )
    parser.add_argument("files", nargs="*", help="explicit files to scan")
    args = parser.parse_args(argv)

    if args.files:
        paths = [Path(name) for name in args.files]
    elif args.staged:
        paths = staged_files()
    else:
        paths = tracked_files()

    findings = scan(paths)
    for path, lineno, rule in findings:
        print(f"{path}:{lineno}: {rule}")
    if findings:
        print(
            f"\n{len(findings)} credential-looking value(s) found. "
            "Replace them with ${ENV_VAR} placeholders and rotate the keys.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
