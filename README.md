# dsh-workers-mcp

An MCP server for driving **long-lived [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) sessions as persistent workers** — dispatch work to them, answer the questions that block them, and inspect what they actually did, from an orchestrating agent in any working directory.

The premise: a `dsh` worker session accumulates expensive orientation (a 57-turn
session here holds ~13 hours of model time at a 97% cache-hit rate). Throwing
that away per task is waste. These tools treat workers as teammates you keep,
not functions you call.

Runtime is **stdlib-only** — no dependencies, including a hand-rolled WebSocket
reader. It is meant to sit beside an agent harness without dragging a dependency
tree into it.

## What it is not

**Not an [A2A](https://a2a-protocol.org/) implementation.** No Agent Cards, no
`message/send`, no Task lifecycle. A2A was evaluated and deliberately declined:
its guiding principle is *opaque execution*, and roughly half the value here is
the opposite — exposing context pressure, cache-hit rates, step counts, turn-end
reasons, and reasoning-block sizes so an orchestrator can supervise closely.
The reasoning, including the three A2A ideas worth borrowing and the conditions
that would change the verdict, is in [`docs/A2A_EVAL.md`](docs/A2A_EVAL.md).

By A2A's own taxonomy this is an agent-to-agent capability delivered over
agent-to-tool transport. That is a real tradeoff, not an oversight: it works for
one orchestrator driving local workers, and stops working the moment a worker
needs to *initiate* contact rather than only respond.

**Not general-purpose.** Every operation is specific to `dsh`'s HTTP API, event
types, and projections. It is not an abstraction over agent frameworks.

## Requirements

- Python ≥ 3.10
- A running `dsh web` server (default `http://127.0.0.1:3080`)

## Install

```bash
uv sync                 # or: pipx install -e .
```

Two entry points:

| Command | Purpose |
|---|---|
| `dsh-workers-mcp` | MCP stdio server |
| `dshctl` | the same operations as a CLI, useful for debugging |

## Wiring it into an agent

```json
{
  "mcpServers": {
    "dsh-workers": {
      "command": "uv",
      "args": ["run", "--project", "/abs/path/to/dsh-workers-mcp", "dsh-workers-mcp"],
      "timeout": 600000
    }
  }
}
```

Use an absolute path. A relative one resolves against the agent's working
directory, which is the whole reason this lives in its own repo.

Use `uv run --project`, not `uv --directory`: `--directory` changes the working
directory to this repo, which moves the default state directory (below) here
instead of into the workspace the agent is working in.

Server flags: `--state-dir <path>` sets where orchestrator state is written;
`--no-state` records nothing and hides `dsh_roster`/`dsh_tasks`;
`--allow-full-access` lets `dsh_permission`/`dsh_create` apply
`danger-full-access` (refused by default).

The companion skill at [`skills/dsh-workers/SKILL.md`](skills/dsh-workers/SKILL.md)
documents the protocol, the operational traps, and compaction semantics. It is a
plain markdown file with frontmatter: load it through whatever your MCP-capable
agent uses for skills or standing instructions (kiro-cli, for example,
references it as `skill:///abs/path/to/dsh-workers-mcp/skills/dsh-workers/SKILL.md`;
a client without skills can include the file in its instructions).

## Permission presets

The method hires workers at a supervision level (`dsh_create permission=…`).
Stock `dsh` does not have those levels, so [`overlays/tool-policy/`](overlays/tool-policy/)
provides them as a dsh patch you can read and edit:

| Preset | Behaviour |
|---|---|
| `read-only` | `write`/`edit` denied. Reviewers and verifiers |
| `supervised` | web access and `git push` ask the lead (`dsh_pending`) |
| `autonomous` | the same are rejected outright, so it never blocks |

```bash
dshctl policy-patch --out ~/.dsh/tool-policy.patch.yml        # fills in this install's path
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile> --dump-config   # does it compose?
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile>
```

The rules are data (`tool`, an argument regex, `allow`/`deny`/`ask`, a reason), so
you add your own; the overlay's README has examples. Presets control **scope,
not security**: reads and the network stay open, and `bash` rules match command
text, which is easy to evade. Status: the patch composes with `--dump-config`;
its live-boot checks (a denied `git push`, an approval raised) come from earlier
testing of the same plugin, not from this release.

## What a worker can and cannot do

Presets and the sandbox stop some things and not others. From testing a
scratch worker at `autonomous`:

| Result | What |
|---|---|
| Enforced by code | writes outside the workspace; `git push`; signalling other processes; API keys in the environment (none are passed to workers) |
| **Not prevented** | **reads** of any file in the home directory, including the dsh settings file that holds provider keys; **writes into another worker's area** (the writable boundary is the whole workspace); **network** (open by design) |

If you run workers unattended, either accept that, or box the dsh server:
[`docs/SANDBOX.md`](docs/SANDBOX.md) has the table in full, a bubblewrap wrapper
(`scripts/dsh-sandbox.sh`, with a self-test), and a **known problem**: dsh's own
per-command sandbox may not start inside it. Read that before relying on it.

## The tools

| Tool | Does | Blocks? |
|---|---|---|
| `dsh_list` | roster: who exists, who is busy, context pressure | no |
| `dsh_status` | deep insight into one worker, incl. `lastTurnEnd` | no |
| `dsh_grep` | regex over one worker's own history, paged backwards | no |
| `dsh_search` | full-text across every session, for attribution | no |
| `dsh_models` | model and reasoning-effort levels; set a worker's effort (`reasoning_effort=`) | no |
| `dsh_pending` | **what a worker is blocked on** | no |
| `dsh_answer` | **unblock it** (a question) | no |
| `dsh_approve` | **unblock it** (a tool approval), after reading the real arguments | no |
| `dsh_task` | dispatch real work | no |
| `dsh_ask` | quick question, wait for the reply | yes |
| `dsh_wait` | wait for a turn to end, or return early when the worker blocks; `sessions=[…]` returns on the first of several | yes |
| `dsh_fork` | fork a session at a turn boundary (recover a poisoned history) | no |
| `dsh_goal` | read or drive a goal: an autonomous loop (`set` needs `start=true`) | no |
| `dsh_name` | pin a role name on a worker, so the roster is legible | no |
| `dsh_create` | create a worker, optionally at a permission preset | no |
| `dsh_permission` | show or set a worker's supervision level | no |
| `dsh_cancel` | interrupt a turn, and confirm the abort landed | briefly |
| `dsh_roster` | managed workers: server-filled facts + your role/notes | no |
| `dsh_tasks` | task records: server-tracked dispatch/return + your verdicts | no |
| `dsh_digest` | what a worker did in its last turn(s), computed mechanically (no model) | no |
| `dsh_brief` | the start-of-session sweep: what needs you, then the roster and open tasks | no |

Three groups carry the weight: **delegate** (`dsh_task`/`dsh_ask`), **report**
(`dsh_status`/`dsh_grep`), **unblock** (`dsh_pending`/`dsh_answer`). The rest
keeps a long-lived roster legible.

Read-only tools are safe to auto-approve. `dsh_task`, `dsh_ask`, `dsh_answer`,
`dsh_goal`, `dsh_name`, `dsh_create`, `dsh_cancel`, `dsh_approve`, and
`dsh_permission` change worker state. For an orchestrator that must keep
workers moving without the user in the loop, auto-approving them is the point,
and the server makes that bounded: it refuses `danger-full-access` (and
permission/sandbox/approval slash commands sent as prompts) unless started
with `--allow-full-access`. So the widest a lead can make a worker is
`autonomous`/`supervised`, whatever it is talked into. `dsh_roster` and `dsh_tasks` only write the
state directory. Gate them deliberately: a worker acts on an
answer or approval with full tool access.

Supervision-level presets (`read-only` / `supervised` / `autonomous`) come from
the [`overlays/tool-policy/`](overlays/tool-policy/) dsh overlay: see
[Permission presets](#permission-presets). They control **scope, not security**.

## The method

[`orchestration/METHOD.md`](orchestration/METHOD.md) is the working method for
a lead agent driving these workers: session start, hiring, the task lifecycle,
when to answer a worker and when to escalate, approvals, merge/release, and a
`FRICTION.md` loop for where the method did not fit. `orchestration/templates/`
holds the per-project files (`PROJECT.md`, `DECISIONS.md`, `FRICTION.md`,
`WORKER_BRIEF.md`) to copy into a workspace's `.orchestrator/`.

## Orchestrator state

The server records what it observes, so the orchestrator cannot forget to. By
default this goes to `.orchestrator/` in the directory the server was started
from; `--state-dir` overrides it. It is created on the first tool call.

```
.orchestrator/
  logs/YYYY-MM-DD.jsonl     every tool call: arguments, result, ok, duration
  history/<sessionId>/
    events.jsonl            incremental mirror of the dsh session log (chunks dropped)
    mirror.json             its lastSeq cursor
    state.json              last observed: last prompt/reply/turn end with times,
                            context, preset, goal, blocked-on
  roster.json               managed workers: `auto` (server) + `notes` (orchestrator)
  tasks.json                tasks: fields, dispatch records, status history
```

The rule is **the server writes facts, the orchestrator writes judgment**, and
judgment only goes through `dsh_roster` / `dsh_tasks`, so neither can clobber
the other:

- **Roster.** `dsh_create`, `dsh_task` and `dsh_ask` put a session on the
  roster; `dsh_roster action=adopt` adds an existing one. Name, preset, model,
  goal, last turn end and current task are filled in from observed calls. Role,
  notes and retired are yours. `dsh_list` reconciles against dsh and reports
  **drift** (renamed or re-preset in the UI, deleted) instead of silently
  disagreeing.
- **Tasks.** `dsh_tasks action=add` returns an id; pass it as `task_id` to
  `dsh_task`. It is checked before anything is sent (unknown, closed, or owned
  by another worker refuses the dispatch). The server then moves it
  `dispatched → blocked → returned` as it observes the worker. `in-review`,
  `merged`, `rejected`, `abandoned` are the orchestrator's calls.
- **The digest is mechanical.** `dsh_digest` (or `dsh_wait … digest=true`, which
  attaches it when a turn ends) summarises the last turn(s) from the mirrored
  event log, with no model call: files edited (from `edit`/`write` calls), shell
  commands by kind, commands that ran as **checks** with ok/failed and their last
  output line, whether a check ran **after the last edit**, errors and
  tool-policy refusals, and, when the task has a worktree, what changed in git
  since the dispatch (the server records the worktree's state at dispatch).
  What counts as a check comes from the task's acceptance line (backticked
  commands), built-in patterns for common runners, and one regex per line in
  `<state dir>/checks.txt`. Shell parsing is a heuristic: `python -c` and heredoc
  scripts show as `opaque`, and a failing run behind `| tail` is recognised from
  its output, since the pipeline hides the exit status. The git section is the
  reliable record of what changed, whatever command did it.
- **Each dispatch records what it used.** With `task_id`, `dsh_task` stores the
  worker's model, reasoning effort and permission on the task, and the turn end
  stores the dispatch's tokens, steps, wall minutes and context change, as dsh
  reports them (a field dsh does not give is omitted, not estimated).
  `dsh_tasks show` adds a rollup; `dsh_tasks action=report` groups tasks by model
  and effort with rounds per task. It reports work, not money: prices change, so
  look them up on the provider's pricing page.
- **Writes answer briefly; `show` has everything.** `dsh_tasks` add/update and
  `dsh_roster` adopt/note/set return a short acknowledgement (id, status, owner,
  branch, note count; for a worker, role, retired, last note), not the whole
  record. The caller just wrote it, and the record, with its history, would
  otherwise sit in the lead's context on every call. `dsh_tasks action=show` and
  `dsh_roster action=show session=…` return the full record.
- **Only managed sessions are mirrored.** Inspecting a session with
  `dsh_status` does not start recording it.
- **Cached state is "last observed".** Every snapshot carries `observedAt`;
  while dsh is up, the live tools are the source of truth.
- **Recording never changes a result.** If writing state fails after a call
  succeeded, the success stands and a `[state: …]` note is appended, so the
  orchestrator never re-sends work that already happened.

Measured: the first mirror of a 12,649-event session (2,500 tool calls) takes
4.6 s and 24 MB; later syncs of an unchanged session take 0.3 s. Keep
`logs/` and `history/` out of version control: they hold full worker
transcripts.

## Two facts worth knowing before you build on this

**A blocked worker is indistinguishable from a finished one.** `dsh`'s
`TurnEndReasonMap` is `completed | aborted | blocked | error | max-tokens |
interrupted` — there is no input-required state. A worker that asked a question
and is waiting ends its turn `completed` and sits `idle`. The signal lives on a
different plane: an answerable server-request frame on the mux stream, which is
what `dsh_pending` reads. Check it whenever a worker goes idle early.

**The event streams are WebSocket, not SSE.** `/api/events.mux` and
`/api/events.host` answer `426 upgrade required` to a plain GET. The `dsh`
source also contains an SSE carrier for the same API; it belongs to a different
deployment. That mistake cost a full working implementation that read zero
frames.

Both are covered in more detail, with source citations, in the skill.

## Tests

```bash
uv run pytest            # or: PYTHONPATH=src python3 -m pytest
```

272 tests, no live server needed. They cover the places where being wrong is
quiet rather than loud: reasoning blocks leaking into replies (a real bug —
reasoning and answer blocks both carry a `text` field), turn-end reasons being
swallowed, answer batching and its non-obvious validation rules, an unreachable
server reporting "nothing pending", a cancel that lost its race reporting
success, WebSocket framing, and the rule that no tool failure may escape
`handle()` and kill the stdio transport.

Every tool has additionally been exercised against a live `dsh` server. Verified
behaviours worth knowing: the block/unblock loop works **unattended**, with no
browser or UI client in the path; an explicitly set title **pins**, surviving
later turns; and `dsh_cancel` separates a real abort from a lost race.

## Provenance

Every transport claim here was verified against a running server and the `dsh`
source, not inferred from documentation. It began as throwaway scripts in an
evaluation workspace and was extracted from there.

## License

MIT
