# Project conventions — <project>

Instance file for `orchestration/METHOD.md`. The lead and the user maintain it.
Keep it to facts a worker or a new lead session needs.

## Where things are

- Workspace root (worker `cwd`): `<path>`
- Vision / design docs: `<paths>`, and which to read first
- Status / roadmap: `<path>`

## Repositories and branches

| Repo | Path | Default branch | Remote |
|---|---|---|---|

Worktree convention: `<how to create one, where it lives, what to copy in>`

## Build and test

- Build: `<command>`
- Test: `<command>`
- Known traps: `<…>`
- Checks the digest should recognise: the digest (`dsh_digest`) already knows common
  runners (`pytest`, `make test`, `gradle test`, `npm test`, `cargo test`, `go test`,
  linters) and any backticked command in a task's acceptance line. List other
  project check commands, one regex per line, in `.orchestrator/checks.txt`.

## What the lead may do without asking

Default (METHOD, *Deciding, and escalating*): everything not listed under
"What needs the user", including design calls within the docs. List here only
what is worth stating explicitly, and the checks that gate it.

- `<e.g. merge verified branches into main>`
- `<e.g. push main of a private repo, when: full suite green on the merge, the
  remote has not moved, the repo is private, nothing secret in the range>`
- `<e.g. start the next roadmap item without asking>`

## What needs the user

Keep it short. Every line here is a place the team stops and waits.

- `<e.g. releases, tags, making a repo public, anything that runs the game window>`
