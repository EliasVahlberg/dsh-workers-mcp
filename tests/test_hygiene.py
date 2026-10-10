"""Tracked files stay free of any one project's or person's names (a release check)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# Words that tie the docs and code to a single project or person. The authors line
# in pyproject.toml and the LICENSE keep the author's name; this file lists the words.
NAMES = re.compile(
    r"\b(necesse|arcane|saltglass|ap-lead|elias|vahlberg|deepseek-harness-eval"
    r"|PlotGraph|MachineCore)\b", re.IGNORECASE)
ALLOWED = {"LICENSE", "pyproject.toml", "tests/test_hygiene.py", "tests/test_overlay.py"}


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(),
                    reason="needs a git checkout")
def test_tracked_files_name_no_project_or_person() -> None:
    files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                           check=True).stdout.split()
    hits = []
    for name in files:
        if name in ALLOWED or name == "uv.lock":
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{name}:{n}: {line.strip()[:80]}"
                 for n, line in enumerate(text.splitlines(), 1) if NAMES.search(line)]
    assert not hits, "\n".join(hits)
