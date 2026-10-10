# Changelog

This file follows [Keep a Changelog](https://keepachangelog.com/). The version is
`__version__` in `src/dsh_workers_mcp/__init__.py`. Change the heading of a release
from `(unreleased)` to its date when you tag it.

## 0.1.0 (unreleased)

The first release. The project is alpha: tool names, tool arguments and the state
format can change. The Status section of the README lists what ran and what did not.

### Added

- **The MCP server** `dsh-workers-mcp`, with 21 tools. A lead agent uses them to hire,
  steer, observe and unblock long-lived dsh worker sessions. The tool tables in the
  docs and in the skill come from one registry.
- **`dshctl`**, the same operations as a command line, for debugging.
- **`dsh-wake`**, an optional loop that runs the client of the lead again when
  something needs it.
- **Orchestrator state** in a directory: the roster, task records with verdicts, a
  mechanical digest of what a worker did, and a start-of-session brief. Recording
  never turns a successful call into a failure.
- **The `tool-policy` overlay** for dsh: supervision-level permission presets with
  allow, deny and ask rules, and optional extra rules. The server refuses
  `danger-full-access` unless it starts with `--allow-full-access`.
- **The method for a lead**: `orchestration/METHOD.md`, `PLAYBOOKS.md`, `REMINDER.md`
  and the project templates, and the `dsh-workers` skill. They follow `docs/STYLE.md`.
- **Optional scripts**: a bubblewrap wrapper for the dsh server
  (`scripts/dsh-sandbox.sh`) and a commit size gate (`scripts/size-gate.sh`).
- **Quick start** in `docs/QUICKSTART.md`, tested end to end against a stand-in dsh.
- **Brand assets** in `assets/`.

### Engineering

- The runtime has no dependencies. It includes a hand-written WebSocket reader.
- All calls to dsh go through one `DshClient` interface. A recording of the response
  shapes (`contracts/dsh-shapes.json`) lets the server report drift of the dsh API.
- Modules import in one direction, and a test enforces the layers.
- A session id must be a plain file name before it becomes a directory in the state
  directory. Task ids already had to look like `T-007`.
- CI runs the tests on Python 3.10 to 3.13, ruff, mypy, and a complexity ratchet.

### Known limits

- Tested against dsh `0.1.1-rc.2`. The dsh API is not a stable interface.
- The calls that change state ran against the stand-in dsh only, since the client
  interface was added.
- The permission presets control scope. They are not a security boundary.
