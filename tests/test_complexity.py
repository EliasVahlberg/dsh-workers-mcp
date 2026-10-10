"""The complexity ratchet (scripts/complexity.py) holds on the current source."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("radon")

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("complexity", ROOT / "scripts" / "complexity.py")
complexity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(complexity)


def test_the_source_matches_the_complexity_baseline() -> None:
    recorded = json.loads((ROOT / "complexity-baseline.json").read_text())
    assert complexity.compare(complexity.measure(), recorded) == []


def test_the_ratchet_rejects_a_new_block_a_rise_and_a_stale_entry() -> None:
    recorded = {"a.py::f": 20, "a.py::g": 15}
    assert complexity.compare({"a.py::f": 20, "a.py::g": 15}, recorded) == []
    assert "new block" in complexity.compare({**recorded, "a.py::h": 11}, recorded)[0]
    assert "went from 20 to 21" in complexity.compare({**recorded, "a.py::f": 21}, recorded)[0]
    assert "went down" in complexity.compare({**recorded, "a.py::f": 12}, recorded)[0]
    assert "gone" in complexity.compare({"a.py::f": 20}, recorded)[0]


def test_blocks_counts_methods_and_closures() -> None:
    source = (
        "class C:\n"
        "    def m(self, x):\n"
        "        if x:\n"
        "            return 1\n"
        "        return 2\n"
        "def outer():\n"
        "    def inner(y):\n"
        "        return 1 if y else 2\n"
        "    return inner\n"
    )
    found = dict(complexity.blocks(source))
    assert found == {"C.m": 2, "outer": 1, "outer.inner": 2}
