"""Check the project's public copy for text that should not ship.

Public copy is what readers see outside the code: the PyPI page (OVERVIEW.md
and pyproject.toml metadata), CHANGELOG.md, CONTRIBUTING.md, CITATION.cff, the
per-connector DATA_TERMS.md files, and commit messages (release notes are
generated from them).

It fails on:

- typographic dashes and curly quotes, as characters or HTML entities
  (house style is plain ASCII);
- a double hyphen used as a dash (`word -- word` or `word--word`; CLI flags
  such as `--upgrade` and Markdown table rules are fine);
- internal tracker ids (`d_`, `t_`, `p_`, `req_`, `br_`, `ho_` followed by a
  hex id);
- internal marker and downstream-private names that must not leak.

Usage:
    python scripts/check_public_copy.py            # check the public copy files
    python scripts/check_public_copy.py FILE ...   # check the named files
    git log --format=%B A..B | python scripts/check_public_copy.py --stdin commits

Exit status 1 when anything is found, 0 when clean.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PUBLIC_COPY = (
    "OVERVIEW.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "CITATION.cff",
    "pyproject.toml",
)
PUBLIC_COPY_GLOBS = ("src/spectraccess/connectors/*/DATA_TERMS.md",)

RULES = (
    (
        re.compile("[\u2012\u2013\u2014\u2015]"),
        "typographic dash; use a comma, colon, parentheses or a plain hyphen",
    ),
    (
        re.compile("[\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u201f]"),
        "curly quote; use a straight quote",
    ),
    (
        re.compile(r"&(?:[mn]dash|[lr][sd]quo);"),
        "HTML entity for a typographic dash or curly quote",
    ),
    (
        re.compile(r"(?:(?<=\s)|^)--(?=\s|$)|(?<=\w)--(?=\w)"),
        "double hyphen used as a dash",
    ),
    (
        re.compile(r"\b(?:d|t|p|req|br|ho)_[0-9a-f]{6,}\b"),
        "internal tracker id",
    ),
    (
        re.compile(r"README_OK|REFCAL_"),
        "internal marker or downstream-private name",
    ),
)


def find_problems(text: str) -> list[tuple[int, int, str, str]]:
    """Return (line, column, message, excerpt) for every rule hit in text."""
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, message in RULES:
            for match in pattern.finditer(line):
                start = max(match.start() - 30, 0)
                excerpt = line[start : match.end() + 30].strip()
                problems.append((lineno, match.start() + 1, message, excerpt))
    return problems


def public_copy_files(root: Path = ROOT) -> list[Path]:
    files = [root / name for name in PUBLIC_COPY]
    for pattern in PUBLIC_COPY_GLOBS:
        files.extend(sorted(root.glob(pattern)))
    return files


def _report(label: str, problems: list[tuple[int, int, str, str]]) -> None:
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for lineno, column, message, excerpt in problems:
        if annotate:
            data = f"{message}: {excerpt}".replace("%", "%25")
            print(f"::error file={label},line={lineno},col={column}::{data}")
        else:
            print(f"{label}:{lineno}:{column}: {message}: {excerpt}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="files to check (default: the public copy)")
    parser.add_argument("--stdin", metavar="LABEL", help="check text read from stdin, reported under LABEL")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")

    sources: list[tuple[str, str]] = []
    if args.stdin:
        # Decode as UTF-8 whatever the platform's console encoding is.
        stream = getattr(sys.stdin, "buffer", None)
        text = stream.read().decode("utf-8", errors="replace") if stream else sys.stdin.read()
        sources.append((args.stdin, text))
    else:
        for path in args.files or public_copy_files():
            if not path.is_file():
                print(f"{path}: expected public copy file is missing", file=sys.stderr)
                return 1
            label = path.resolve().relative_to(ROOT).as_posix() if path.resolve().is_relative_to(ROOT) else str(path)
            sources.append((label, path.read_text(encoding="utf-8")))

    found = 0
    for label, text in sources:
        problems = find_problems(text)
        _report(label, problems)
        found += len(problems)

    checked = ", ".join(label for label, _ in sources)
    if found:
        print(f"{found} problem(s) in public copy ({len(sources)} source(s) checked).", file=sys.stderr)
        return 1
    print(f"Public copy clean: {checked}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
