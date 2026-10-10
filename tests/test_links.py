"""Every relative link in a tracked markdown file points to a file, and to a heading that exists."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^```.*?^```", re.M | re.S)
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.M)


def markdown_files() -> list[Path]:
    if shutil.which("git") and (ROOT / ".git").exists():
        out = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.split()
        return [ROOT / name for name in out]
    return [p for p in ROOT.rglob("*.md") if ".venv" not in p.parts]


def slug(heading: str) -> str:
    """The anchor that GitHub makes from a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    return {slug(match) for match in HEADING.findall(text)}


def links(path: Path) -> list[str]:
    text = FENCE.sub("", path.read_text(encoding="utf-8"))
    found = LINK.findall(text)
    return [t for t in found if not re.match(r"^([a-z][a-z0-9+.-]*:|/|<)", t, re.I)]


def problems(path: Path) -> list[str]:
    out = []
    for target in links(path):
        name, _, anchor = target.partition("#")
        dest = (path.parent / name).resolve() if name else path
        where = f"{path.relative_to(ROOT)}: {target}"
        if not dest.exists():
            out.append(f"{where} (no such file)")
        elif anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            out.append(f"{where} (no such heading)")
    return out


@pytest.mark.parametrize("path", markdown_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_each_relative_link_resolves(path: Path) -> None:
    assert problems(path) == []


def test_the_checker_finds_a_broken_link_and_a_broken_anchor(tmp_path) -> None:
    (tmp_path / "a.md").write_text("# Title\n\n## Two words\n")
    page = tmp_path / "b.md"
    page.write_text("[ok](a.md) [ok2](a.md#two-words) [bad](c.md) [bad2](a.md#nope) [web](https://x.y)\n"
                    "```\n[in code](nowhere.md)\n```\n")
    global ROOT
    saved, ROOT = ROOT, tmp_path
    try:
        assert problems(page) == ["b.md: c.md (no such file)", "b.md: a.md#nope (no such heading)"]
    finally:
        ROOT = saved
