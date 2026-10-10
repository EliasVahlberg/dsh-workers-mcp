# Project conventions: <project>

This is the instance file for `orchestration/METHOD.md`. The lead and the user
maintain it. Write only facts that a worker or a new lead session needs.

## Where things are

- Workspace root (the worker `cwd`): `<path>`
- Vision and design documents: `<paths>`, and which to read first
- Status or roadmap: `<path>`

## Repositories and branches

| Repo | Path | Default branch | Remote |
|---|---|---|---|

Worktree convention: `<how to create a worktree, where it lives, and what to
copy into it>`

## Build and test

- Build: `<command>`
- Test: `<command>`
- Models: `<which model and effort to use for debugging and design, and which for routine pieces>`
- Known traps: `<…>`
- Found only by hand: `<Paths that a headless suite cannot reach, for example
  rendering, a real browser or a device. List them, so that nobody reads a
  green suite as covering them. Keep any source-shape guard in the project,
  not here.>`
- Checks that the digest should recognise: `dsh_digest` already knows common
  runners (`pytest`, `make test`, `gradle test`, `npm test`, `cargo test`,
  `go test`, linters) and any backticked command in a task's acceptance line.
  List other check commands of the project in `.orchestrator/checks.txt`, one
  regex per line.

## What the lead may do without asking

Default (METHOD, *Deciding, and escalating*): the lead may do everything that
is not under "What needs the user", including design decisions that the
documents cover. List here only what is worth stating, and the checks that
gate it.

- `<for example: merge verified branches into main>`
- `<for example: push main of a private repo, when the full suite is green on
  the merge, the remote has not moved, the repo is private, and the range has
  no secret>`
- `<for example: start the next roadmap item without asking>`

## What needs the user

Keep this list short. At every line, the team stops and waits.

- `<for example: releases, tags, making a repo public, anything that opens a GUI
  or a browser window>`
