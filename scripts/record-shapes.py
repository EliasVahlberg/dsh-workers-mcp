#!/usr/bin/env python3
"""
Record the shape of real dsh responses into contracts/dsh-shapes.json.

Run it against a live `dsh web`:

    uv run python scripts/record-shapes.py [--base http://127.0.0.1:3080]

It makes read-only calls (session.list, session.history, session.models,
host.describe). It stores the keys and types of each response, never the
values, so the file holds no session content and no secrets. It also records
the dsh package version and git commit when the server's working directory is a
dsh checkout. A test checks that `shape.REQUIRED` fits the recording.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dsh_workers_mcp.shape import skeleton  # noqa: E402
from dsh_workers_mcp.transport import DEFAULT_BASE, HttpClient, RpcError  # noqa: E402

OUT = ROOT / "contracts" / "dsh-shapes.json"


def checkout_facts(cwd: str) -> dict:
    """Package version and commit of the dsh checkout at `cwd`, when it is one."""
    root = Path(cwd)
    if not (root / "packages" / "host" / "apiproxy").is_dir():
        return {}
    facts: dict = {}
    try:
        facts["package"] = json.loads((root / "package.json").read_text()).get("version")
        facts["commit"] = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                                         capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, ValueError, subprocess.CalledProcessError):
        pass
    return facts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", default=DEFAULT_BASE)
    args = parser.parse_args(argv)
    client = HttpClient(args.base)
    try:
        describe = client.rpc("host.describe")
        listing = client.rpc("session.list")
        shapes = {"host.describe": skeleton(describe), "session.list": skeleton(listing)}
        items = listing.get("items") or []
        if items:
            sid = items[0]["sessionId"]
            shapes["session.history"] = skeleton(client.rpc(
                "session.history", {"sessionId": sid, "maxMessages": 40}))
            shapes["session.models"] = skeleton(client.rpc("session.models", {"sessionId": sid}))
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    document = {"dsh": checkout_facts(describe.get("cwd", "")), "shapes": shapes}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {sorted(shapes)}; dsh {document['dsh'] or 'not a checkout'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
