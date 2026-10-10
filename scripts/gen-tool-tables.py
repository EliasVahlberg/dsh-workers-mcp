#!/usr/bin/env python3
"""
Write the tool table in the README and in the skill from the tool registry.

The table sits between `<!-- tools:start -->` and `<!-- tools:end -->` in each file.
Run `uv run python scripts/gen-tool-tables.py` after you change a tool, and commit the
result. `--check` changes nothing and exits 1 when a table is out of date. A test runs
the same check.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dsh_workers_mcp.tools import all_tools  # noqa: E402

TARGETS = [ROOT / "README.md", ROOT / "skills" / "dsh-workers" / "SKILL.md"]
START, END = "<!-- tools:start -->", "<!-- tools:end -->"


def table() -> str:
    rows = ["| Tool | Does | Blocks? |", "|---|---|---|"]
    rows += [f"| `{tool.name}` | {tool.summary} | {tool.blocks} |" for tool in all_tools()]
    return "\n".join(rows)


def render(text: str) -> str:
    """`text` with the part between the markers replaced by the current table."""
    before, found, rest = text.partition(START)
    _old, found_end, after = rest.partition(END)
    if not (found and found_end):
        raise SystemExit("the markers are missing")
    return f"{before}{START}\n{table()}\n{END}{after}"


def stale() -> list[Path]:
    return [path for path in TARGETS if render(path.read_text()) != path.read_text()]


def main(argv: list[str]) -> int:
    if "--check" in argv:
        out_of_date = stale()
        for path in out_of_date:
            print(f"{path.relative_to(ROOT)}: the tool table is out of date")
        return 1 if out_of_date else 0
    for path in TARGETS:
        path.write_text(render(path.read_text()))
        print(f"wrote the tool table in {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
