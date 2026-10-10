"""The tool tables in docs/TOOLS.md and the skill are generated from the tool registry."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("gen_tool_tables", ROOT / "scripts" / "gen-tool-tables.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


def test_the_tables_in_the_docs_match_the_registry() -> None:
    stale = [str(path.relative_to(ROOT)) for path in gen.stale()]
    assert stale == [], f"run: uv run python scripts/gen-tool-tables.py ({stale})"


def test_every_tool_is_in_each_table_once() -> None:
    from dsh_workers_mcp.tools import all_tools
    for path in gen.TARGETS:
        text = path.read_text()
        block = text.split(gen.START)[1].split(gen.END)[0]
        for tool in all_tools():
            assert block.count(f"| `{tool.name}` |") == 1, (path.name, tool.name)


def test_render_replaces_only_what_is_between_the_markers() -> None:
    original = f"before\n{gen.START}\nold table\n{gen.END}\nafter\n"
    rendered = gen.render(original)
    assert rendered.startswith("before\n") and rendered.endswith("\nafter\n") and "old table" not in rendered
    assert gen.render(rendered) == rendered                     # running it twice changes nothing


def test_render_refuses_a_file_without_markers() -> None:
    with pytest.raises(SystemExit):
        gen.render("no markers here")
