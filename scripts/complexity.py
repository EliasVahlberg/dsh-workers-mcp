#!/usr/bin/env python3
"""
Cyclomatic complexity ratchet.

`complexity-baseline.json` records every function and method in src/ whose
complexity is 11 or more (radon rank C and worse), with its number. The check
fails when:
  * a block reaches 11 or more and is not in the baseline,
  * a recorded block has a higher number than recorded,
  * a recorded block has a lower number than recorded, or is gone. The baseline
    is stale then. Run `scripts/complexity.py --update` and commit the file, so
    that the gain cannot be lost later.

Usage:
  python scripts/complexity.py            # check, exit 1 on a failure
  python scripts/complexity.py --update   # rewrite the baseline

Needs radon (a dev dependency).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from radon.complexity import cc_visit

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "complexity-baseline.json"
THRESHOLD = 11            # radon rank C starts here


def blocks(source: str):
    """Yield (name, complexity) for every function and method, closures included."""

    def walk(items, prefix=""):
        for item in items:
            if hasattr(item, "methods"):               # a class: its methods come as their own items
                continue
            name = f"{prefix}{item.classname + '.' if item.classname else ''}{item.name}"
            yield name, item.complexity
            yield from walk(getattr(item, "closures", []), f"{name}.")

    yield from walk(cc_visit(source))


def measure() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted((ROOT / "src").rglob("*.py")):
        for name, complexity in blocks(path.read_text()):
            if complexity >= THRESHOLD:
                found[f"{path.relative_to(ROOT).as_posix()}::{name}"] = complexity
    return found


def compare(found: dict[str, int], recorded: dict[str, int]) -> list[str]:
    problems = []
    for key, value in sorted(found.items()):
        if key not in recorded:
            problems.append(f"new block above the threshold: {key} has complexity {value}")
        elif value > recorded[key]:
            problems.append(f"{key} went from {recorded[key]} to {value}")
        elif value < recorded[key]:
            problems.append(f"{key} went down from {recorded[key]} to {value}: "
                            "run scripts/complexity.py --update")
    for key in sorted(set(recorded) - set(found)):
        problems.append(f"{key} is below the threshold or gone: run scripts/complexity.py --update")
    return problems


def main(argv: list[str]) -> int:
    found = measure()
    if "--update" in argv:
        BASELINE.write_text(json.dumps(dict(sorted(found.items())), indent=2) + "\n")
        print(f"wrote {BASELINE.name}: {len(found)} blocks")
        return 0
    problems = compare(found, json.loads(BASELINE.read_text()))
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
