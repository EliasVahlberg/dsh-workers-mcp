# Development: the checks, and what the tests cover

[`CONTRIBUTING.md`](../CONTRIBUTING.md) has the rules. This page has the detail behind them.

```bash
uv run pytest            # or: PYTHONPATH=src python3 -m pytest
uv run ruff check src tests scripts
uv run mypy
uv run python scripts/complexity.py
uv run python scripts/gen-tool-tables.py --check
```

CI runs these on every push and pull request, with the tests on Python 3.10, 3.11, 3.12 and 3.13. `scripts/complexity.py` is a complexity ratchet. `complexity-baseline.json` records each function with a cyclomatic complexity of 11 or more. A new function above that, or a rise in a recorded one, fails the check. After you lower one, run `uv run python scripts/complexity.py --update` and commit the file.

The modules import each other in one direction only. The docstring of [`src/dsh_workers_mcp/__init__.py`](../src/dsh_workers_mcp/__init__.py) lists the layers, and `tests/test_imports.py` fails on an import that points up or sideways. To share code between two modules, move it to a lower layer.

To add a tool, add one `Tool` object to `tools/session_tools.py`, `tools/work_tools.py` or `tools/state_tools.py`. It holds the name, the description, the schema, the handler, and a one-line summary. Then run `uv run python scripts/gen-tool-tables.py` to update the tool tables in [`TOOLS.md`](TOOLS.md) and in the skill. `tests/test_tool_tables.py` fails when a table is out of date.

The tests need no live server. They use a stand-in dsh (`tests/fakes.py`, and `tests/fake_http.py` for the wire). They cover the places where a mistake is quiet and not loud:
- reasoning blocks that leak into replies (a real bug: reasoning blocks and answer blocks both carry a `text` field)
- turn-end reasons that nothing reports
- answer batching and its validation rules, which are not obvious
- an unreachable server that reports "nothing pending"
- a cancel that lost its race and reports success
- WebSocket framing
- the rule that no tool failure may escape `handle()` and kill the stdio transport
- a state directory that an older version wrote (`tests/fixtures/state-v1`)
- ids with path characters (`tests/test_state_paths.py`)

What ran against a live dsh is in [`DSH_API.md`](DSH_API.md).
