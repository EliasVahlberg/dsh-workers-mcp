"""One version, one changelog section, and a release check that stops a wrong tag."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from dsh_workers_mcp import __version__, server

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts" / "release-notes.py")
notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(notes)

SAMPLE = """# Changelog

## 1.2.0 (unreleased)

Next.

## [1.1.0] - 2026-01-02

### Added

- a thing

## 1.0.0 - 2025-12-01

First.
"""


def test_the_version_has_one_source() -> None:
    """pyproject.toml reads the version from the package, and the server reports the same one."""
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert not re.search(r'^version\s*=', pyproject, re.M)
    assert server.SERVER_INFO["version"] == __version__ == notes.package_version()


def test_the_changelog_has_a_section_for_the_current_version() -> None:
    found = notes.section((ROOT / "CHANGELOG.md").read_text(), __version__)
    assert found is not None and found[1], "add a section to CHANGELOG.md"


def test_section_finds_each_heading_style_and_stops_at_the_next() -> None:
    assert notes.section(SAMPLE, "1.2.0") == ("1.2.0 (unreleased)", "Next.")
    heading, body = notes.section(SAMPLE, "1.1.0")
    assert heading == "1.1.0 - 2026-01-02" and body.endswith("- a thing") and "First" not in body
    assert notes.section(SAMPLE, "1.0") is None             # no prefix match
    assert notes.section(SAMPLE, "9.9.9") is None


@pytest.mark.parametrize(("tag", "version", "expect"), [
    ("v1.1.0", "1.1.0", []),
    (None, "1.2.0", []),                                      # no tag: only the section must exist
    ("v1.1.1", "1.1.0", ["expected v1.1.0"]),
    ("1.1.0", "1.1.0", ["expected v1.1.0"]),
    ("v1.2.0", "1.2.0", ["still says unreleased"]),
    ("v9.9.8", "9.9.9", ["expected", "no section"]),
])
def test_the_release_check_catches_a_wrong_tag(tag, version, expect) -> None:
    errors = notes.problems(tag, version, SAMPLE)
    assert len(errors) == len(expect)
    assert all(part in error for part, error in zip(expect, errors, strict=True))
