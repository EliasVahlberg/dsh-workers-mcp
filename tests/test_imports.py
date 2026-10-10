"""
The modules of the package import each other in one direction only.

Each module has a layer. A module may import only from lower layers, so there is
no cycle and a change in a low layer never needs a change in a higher one to
compile. Imports under `if TYPE_CHECKING:` do not count, because they do not run.

To add a module, give it a layer here. A new import that points up or sideways
fails this test. The fix is to move the shared code down, not to change a layer.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "dsh_workers_mcp"
PACKAGE = "dsh_workers_mcp"

LAYERS = {
    # the wire, and code that needs nothing else
    "transport": 0, "digest": 0,
    # reading dsh through a client
    "sessions": 1, "shape": 1, "state.store": 1,
    "pending": 2, "permissions": 2, "models": 2, "state.usage": 2,
    "waiting": 3, "state.history": 3, "state.roster": 3, "state.tasks": 3,
    "state.core": 4,
    "state.reports": 5,
    "state.hooks": 6,
    "state": 7,
    # the commands, the program built on the state, and the entry points
    "cmd_sessions": 4, "cmd_work": 4,
    "dshctl": 5,
    "brief": 8,
    "server": 9, "wake": 9,
    "__init__": 0,
}


def module_name(path: Path) -> str:
    relative = path.relative_to(SRC).with_suffix("")
    parts = [p for p in relative.parts if p != "__init__"] or ["__init__"]
    return ".".join(parts)


def runtime_imports(path: Path) -> set[str]:
    """The package modules that a file imports when it runs."""
    tree = ast.parse(path.read_text())
    skipped = {id(node) for branch in ast.walk(tree)
               if isinstance(branch, ast.If) and "TYPE_CHECKING" in ast.unparse(branch.test)
               for node in ast.walk(branch)}
    found: set[str] = set()
    for node in ast.walk(tree):
        if id(node) in skipped:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith(PACKAGE):
            base = node.module.removeprefix(PACKAGE).lstrip(".")
            for alias in node.names:
                found.add(".".join(p for p in (base, alias.name) if p))
        elif isinstance(node, ast.Import):
            found |= {a.name.removeprefix(PACKAGE).lstrip(".") for a in node.names
                      if a.name.startswith(PACKAGE)}
    return found


def resolve(name: str) -> str | None:
    """The layered module that an imported name belongs to ('state.store', not 'state.store.Store')."""
    parts = name.split(".")
    for end in range(len(parts), 0, -1):
        candidate = ".".join(parts[:end])
        if candidate in LAYERS:
            return candidate
    return None


def test_every_module_has_a_layer() -> None:
    modules = {module_name(p) for p in SRC.rglob("*.py")}
    assert modules <= set(LAYERS), f"give these modules a layer: {sorted(modules - set(LAYERS))}"
    assert set(LAYERS) - modules == set(), "a layer is named for a module that does not exist"


def test_modules_import_only_from_lower_layers() -> None:
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        me = module_name(path)
        for name in runtime_imports(path):
            target = resolve(name)
            if target is None or target == me:
                continue
            if me == "state" and target.startswith("state."):
                continue                                  # the package re-exports its own modules
            if LAYERS[target] >= LAYERS[me]:
                problems.append(f"{me} (layer {LAYERS[me]}) imports {target} (layer {LAYERS[target]})")
    assert problems == []


def test_the_checker_sees_an_upward_import_and_ignores_type_checking(tmp_path) -> None:
    sample = tmp_path / "m.py"
    sample.write_text(
        "from typing import TYPE_CHECKING\n"
        "from dsh_workers_mcp import server, state as st\n"
        "from dsh_workers_mcp.state.store import Store\n"
        "if TYPE_CHECKING:\n"
        "    from dsh_workers_mcp.state.core import State\n")
    assert runtime_imports(sample) == {"server", "state", "state.store.Store"}
    assert resolve("state.store.Store") == "state.store" and resolve("nothing") is None
