#!/usr/bin/env python3
"""
Print the release notes of the current version, taken from CHANGELOG.md.

    python scripts/release-notes.py                 print the notes
    python scripts/release-notes.py --tag v0.1.0    also check that the tag fits

With --tag the script exits 1 when the tag is not `v` plus the version, when the
changelog has no section for the version, or when that section still says
"unreleased". The release workflow runs it with the tag, so a wrong tag stops the
release before anything is published.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "src" / "dsh_workers_mcp" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"


def package_version(text: str | None = None) -> str:
    text = VERSION_FILE.read_text() if text is None else text
    match = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("no __version__ in the package")
    return match.group(1)


def section(changelog: str, version: str) -> tuple[str, str] | None:
    """(heading, body) of the section for `version`, or None."""
    pattern = re.compile(
        rf"^## \[?{re.escape(version)}(?=[\]\s])\]?(?P<rest>[^\n]*)\n(?P<body>.*?)(?=^## |\Z)",
        re.M | re.S)
    match = pattern.search(changelog)
    if not match:
        return None
    return f"{version}{match.group('rest')}".strip(), match.group("body").strip()


def problems(tag: str | None, version: str, changelog: str) -> list[str]:
    found = section(changelog, version)
    out = []
    if tag is not None and tag != f"v{version}":
        out.append(f"the tag is {tag}, and the package version is {version} (expected v{version})")
    if found is None:
        out.append(f"CHANGELOG.md has no section for {version}")
    elif tag is not None and "unreleased" in found[0].lower():
        out.append(f"the section for {version} still says unreleased: put the date in the heading")
    return out


def main(argv: list[str]) -> int:
    tag = argv[argv.index("--tag") + 1] if "--tag" in argv else None
    version, changelog = package_version(), CHANGELOG.read_text()
    errors = problems(tag, version, changelog)
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    found = section(changelog, version)
    if found:
        print(found[1])
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
