# Contributing

Thank you for helping. These are the rules that keep the project small.

## Set up and check

```bash
uv sync
uv run pytest
uv run ruff check src tests scripts
uv run mypy
uv run python scripts/complexity.py
uv run python scripts/gen-tool-tables.py --check
```

CI runs the same commands. A change is ready when all of them pass. No live dsh
server is needed: the tests use a stand-in (`tests/fakes.py`, `tests/fake_http.py`).

## Rules

- **The runtime has no dependencies.** Use the standard library. A development
  dependency is fine. A runtime dependency needs a reason that outweighs the cost.
- **Imports go one way.** `src/dsh_workers_mcp/__init__.py` lists the layers.
  `tests/test_imports.py` fails on an import that points up or sideways. To share
  code between two modules, move it to a lower layer.
- **Only `transport.py` talks to dsh.** Everything else uses the `DshClient`
  interface. Tests pass a fake client.
- **One tool is one object.** To add a tool, add a `Tool` to `tools/session_tools.py`,
  `tools/work_tools.py` or `tools/state_tools.py`. Then run
  `uv run python scripts/gen-tool-tables.py` and commit the tables.
- **Complexity does not grow.** A function above complexity 10 fails
  `scripts/complexity.py`. If you lower one, run `scripts/complexity.py --update`.
- **Text for agents follows [`docs/STYLE.md`](docs/STYLE.md).** Tool descriptions,
  the method, the playbooks, the skill and the templates are read by agents.
- **No names of people or projects** in tracked files. `tests/test_hygiene.py`
  checks this. The authors line and the license keep the author's name.
- **State what you verified.** In a commit message or a doc, say what ran against a
  live dsh and what ran only against the fake.

## Brand assets

Do not edit `assets/banner.svg`, `assets/social.svg` or `assets/social.png` by hand.
[`assets/README.md`](assets/README.md) says how to rebuild them.

## A change to a dsh call

The dsh API is not a stable interface. If you change what the code reads from a
response, update `shape.REQUIRED`. To record the shapes of another dsh version,
run `uv run python scripts/record-shapes.py`. It makes read-only calls.

## Commits

Write the subject as `type(scope): what changed`. Use the body for why. Keep one
concern in each commit.
