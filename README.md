# dsh-workers-mcp

An MCP server that drives **long-lived [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`) sessions as persistent workers**. An orchestrating agent in any working directory uses it to dispatch work to the workers, answer the questions that block them, and inspect what they did.

The premise: a `dsh` worker session builds up orientation, and orientation is expensive. In testing, a 57-turn session held about 13 hours of model time at a 97% cache-hit rate. To throw that away for each task is waste. These tools treat workers as teammates that you keep. They do not treat workers as functions that you call.

The runtime is **stdlib-only**. It has no dependencies, and that includes a WebSocket reader that we wrote by hand. The goal is that it can sit beside an agent harness and not add a dependency tree to it.

**New here?** Follow [`docs/QUICKSTART.md`](docs/QUICKSTART.md). It takes one worker through one task in about 15 minutes.

## What it is not

**Not an [A2A](https://a2a-protocol.org/) implementation.** It has no Agent Cards, no `message/send` and no Task lifecycle. We evaluated A2A and declined it on purpose. Its guiding principle is *opaque execution*. About half of the value here is the opposite: the tools show context pressure, cache-hit rates, step counts, turn-end reasons and reasoning-block sizes, so that an orchestrator can supervise closely. [`docs/A2A_EVAL.md`](docs/A2A_EVAL.md) gives the reasoning. It also names the three A2A ideas that are worth borrowing, and the conditions that would change the verdict.

In the taxonomy of A2A itself, this is an agent-to-agent capability that runs over an agent-to-tool transport. That is a real tradeoff, not an oversight. It works for one orchestrator that drives local workers. It stops working when a worker must *start* contact and not only respond.

**Not general-purpose.** Every operation is specific to the HTTP API, the event types and the projections of `dsh`. It is not an abstraction over agent frameworks.

## Requirements

- Python 3.10 or later
- A running `dsh web` server (default `http://127.0.0.1:3080`)

## Install

```bash
uv sync                 # or: pipx install -e .
```

There are three entry points:

| Command | Purpose |
|---|---|
| `dsh-workers-mcp` | the MCP stdio server |
| `dshctl` | the same operations as a CLI, useful for debugging |
| `dsh-wake` | optional loop that runs the client of the lead again when something needs it (see [Waking the lead](#waking-the-lead)) |

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

Use an absolute path. A relative path resolves against the working directory of the agent. That is the reason this server lives in its own repo.

Use `uv run --project`. Do not use `uv --directory`. The option `--directory` changes the working directory to this repo. That moves the default state directory (below) here, and out of the workspace where the agent works.

The server flags:
- `--base <url>` sets the dsh server (default `http://127.0.0.1:3080`).
- `--state-dir <path>` sets where the server writes orchestrator state.
- `--no-state` records nothing and hides `dsh_roster` and `dsh_tasks`.
- `--allow-full-access` lets `dsh_permission` and `dsh_create` apply `danger-full-access`. The server refuses it by default.

The companion skill at [`skills/dsh-workers/SKILL.md`](skills/dsh-workers/SKILL.md) documents the protocol, the operational traps and the semantics of compaction. It is a plain markdown file with frontmatter. Load it with whatever your MCP-capable agent uses for skills or standing instructions. For example, kiro-cli references it as `skill:///abs/path/to/dsh-workers-mcp/skills/dsh-workers/SKILL.md`. A client without skills can include the file in its instructions.

## Permission presets

The method hires workers at a supervision level (`dsh_create permission=…`). Stock `dsh` does not have those levels. So [`overlays/tool-policy/`](overlays/tool-policy/) provides them as a dsh patch that you can read and edit:

| Preset | Behaviour |
|---|---|
| `read-only` | `write` and `edit` are denied. For reviewers and verifiers |
| `supervised` | web access and `git push` ask the lead (`dsh_pending`) |
| `autonomous` | the same actions are rejected outright, so the worker never blocks |

```bash
dshctl policy-patch --out ~/.dsh/tool-policy.patch.yml        # fills in the path of this install
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile> --dump-config   # does it compose?
dsh --patch ~/.dsh/tool-policy.patch.yml --profile <profile>
```

The rules are data: a `tool`, an argument regex, `allow`, `deny` or `ask`, and a reason. So you can add your own. The README of the overlay has examples, and [`RECIPES.md`](overlays/tool-policy/RECIPES.md) has optional rules. Presets control **scope, not security**. Reads and the network stay open, and `bash` rules match command text, which is easy to evade. Status: the patch composes with `--dump-config`. The live-boot checks (a denied `git push`, an approval raised) come from earlier testing of the same plugin. They do not come from this release.

## What a worker can and cannot do

Presets and the sandbox stop some things and not others. This is from testing a scratch worker at `autonomous`:

| Result | What |
|---|---|
| Enforced by code | writes outside the workspace, `git push`, signals to other processes, API keys in the environment (the server passes none to workers) |
| **Not prevented** | **reads** of any file in the home directory, including the dsh settings file that holds provider keys. **Writes into the area of another worker** (the writable boundary is the whole workspace). **Network access** (open by design) |

If you run workers unattended, you can accept this, or you can box the dsh server. [`docs/SANDBOX.md`](docs/SANDBOX.md) has the full table and a bubblewrap wrapper (`scripts/dsh-sandbox.sh`, with a self-test). It also describes a **known problem**: the per-command sandbox of dsh itself may not start inside the box. Read that before you rely on it.

## The tools

| Tool | Does | Blocks? |
|---|---|---|
| `dsh_list` | the roster: who exists, who is busy, context pressure | no |
| `dsh_status` | deep insight into one worker, including `lastTurnEnd` | no |
| `dsh_grep` | regex over the own history of one worker, paged backwards | no |
| `dsh_search` | full-text search across every session, to find who discussed a topic | no |
| `dsh_models` | model and reasoning-effort levels. Sets the effort of a worker (`reasoning_effort=`) | no |
| `dsh_pending` | **what a worker is blocked on** | no |
| `dsh_answer` | **unblock it** (a question) | no |
| `dsh_approve` | **unblock it** (a tool approval), after you read the real arguments | no |
| `dsh_task` | dispatch real work | no |
| `dsh_ask` | ask a quick question and wait for the reply | yes |
| `dsh_wait` | wait for a turn to end, or return early when the worker blocks. `sessions=[…]` returns for the first of several workers | yes |
| `dsh_fork` | fork a session at a turn boundary (recovers a poisoned history) | no |
| `dsh_goal` | read or drive a goal, which is an autonomous loop (`set` needs `start=true`) | no |
| `dsh_name` | pin a role name on a worker, so that the roster is readable | no |
| `dsh_create` | create a worker, optionally at a permission preset | no |
| `dsh_permission` | show or set the supervision level of a worker | no |
| `dsh_cancel` | interrupt a turn, and confirm that the abort landed | briefly |
| `dsh_roster` | managed workers: facts that the server fills in, plus your role and notes | no |
| `dsh_tasks` | task records: dispatch and return as the server tracks them, plus your verdicts | no |
| `dsh_digest` | what a worker did in its last turns, computed mechanically (no model) | no |
| `dsh_brief` | the start-of-session sweep: what needs you, then the roster and the open tasks | no |

Three groups carry the weight: **delegate** (`dsh_task` and `dsh_ask`), **report** (`dsh_status` and `dsh_grep`), and **unblock** (`dsh_pending` and `dsh_answer`). The other tools keep a long-lived roster readable.

Read-only tools are safe to auto-approve. These tools change worker state: `dsh_task`, `dsh_ask`, `dsh_answer`, `dsh_goal`, `dsh_name`, `dsh_create`, `dsh_cancel`, `dsh_approve` and `dsh_permission`. An orchestrator that must keep workers moving without the user in the loop needs to auto-approve them. The server limits what that allows. It refuses `danger-full-access`, and it refuses the slash commands for permission, sandbox and approval that arrive as prompts, unless it started with `--allow-full-access`. So the widest permission that a lead can give a worker is `autonomous` or `supervised`, whatever the lead is talked into. `dsh_roster` and `dsh_tasks` write only the state directory. Gate them with care, because a worker acts on an answer or an approval with full tool access.

The supervision-level presets (`read-only`, `supervised`, `autonomous`) come from the dsh overlay in [`overlays/tool-policy/`](overlays/tool-policy/). See [Permission presets](#permission-presets). They control **scope, not security**.

## The method

[`orchestration/METHOD.md`](orchestration/METHOD.md) is the working method for a lead agent that drives these workers. It covers the session start, hiring, the task lifecycle, when to answer a worker and when to escalate, approvals, merge and release, and a `FRICTION.md` loop for the places where the method did not fit. `orchestration/templates/` holds the files for each project (`PROJECT.md`, `DECISIONS.md`, `FRICTION.md`, `WORKER_BRIEF.md`). Copy them into the `.orchestrator/` of a workspace. [`orchestration/REMINDER.md`](orchestration/REMINDER.md) is a one-page copy of the rules that matter most. Load it as a standing resource of the lead, so that a compaction does not remove them.

Three optional pieces are off by default. [`scripts/dsh-sandbox.sh`](scripts/dsh-sandbox.sh) boxes the dsh server ([`docs/SANDBOX.md`](docs/SANDBOX.md)). [`scripts/size-gate.sh`](scripts/size-gate.sh) refuses a commit that takes a task past its size limit (PLAYBOOKS §3). [`overlays/tool-policy/RECIPES.md`](overlays/tool-policy/RECIPES.md) has extra policy rules.

## Waking the lead

The lead runs only when a person types, so a worker that returns or blocks waits for the user. `dsh-wake` is a thin loop that closes this gap. It reads the same data as `dsh_brief`. When `needsAction` has items that it did not show the lead at the last wake, it runs a command that you give it. The command starts the client of the lead non-interactively with a resume prompt.

```bash
dsh-wake --cmd 'my-client --resume --no-interactive {prompt}' \
         --state-dir .orchestrator --milestone MILESTONE.md \
         --budget-hours 8 --max-fires 50 --interval 60
dsh-wake --cmd '…' --dry-run      # one pass: print what it would run, run nothing
```

`{prompt}` and `{state_dir}` are filled in and quoted for the shell. The driver is not tied to one client. The rules:
- It wakes the lead only if `needsAction` has lines, and only if they differ from the lines at the last wake. A lead that cannot clear an item (it waits for the user) is not woken again for that item.
- It stops when the first line of the milestone file starts with `DONE` or `BLOCKED`, after `--budget-hours` hours, or after `--max-fires` wakes.
- It logs every wake, skip, stop and command exit, with the reason, to `<state dir>/wake.jsonl`.
- A wake is a hint. The woken lead starts with `dsh_brief`, and the default prompt says so.

Not verified against a real client. How a non-interactive run treats a tool call that is not pre-approved, and whether the resume flag keeps the context of the lead across wakes, depend on the client. The command allow-list of the lead may need widening for the build and VCS commands of the project. That is a decision for the user. The tests cover the rules, the loop with a fake clock and runner, and the quoting. Try your command by hand before you leave the driver running.

## Orchestrator state

The server records what it observes, so that the orchestrator cannot forget to. By default it writes to `.orchestrator/` in the directory where the server started. `--state-dir` overrides this. The server creates the directory on the first tool call.

```
.orchestrator/
  logs/YYYY-MM-DD.jsonl     every tool call: arguments, result, ok, duration
  history/<sessionId>/
    events.jsonl            incremental mirror of the dsh session log (chunks dropped)
    mirror.json             its lastSeq cursor
    state.json              last observed: last prompt, reply and turn end with times,
                            context, preset, goal, blocked-on
  roster.json               managed workers: `auto` (server) + `notes` (orchestrator)
  tasks.json                tasks: fields, dispatch records, status history
```

The rule is **the server writes facts, the orchestrator writes judgment**. Judgment goes only through `dsh_roster` and `dsh_tasks`, so neither side can overwrite the other:

- **Roster.** `dsh_create`, `dsh_task` and `dsh_ask` put a session on the roster. `dsh_roster action=adopt` adds an existing session. The server fills in the name, preset, model, goal, last turn end and current task from the calls that it observes. The role, the notes and the retired flag are yours. `dsh_list` compares the roster with dsh and reports **drift** (a session that someone renamed or gave another preset in the UI, or deleted). It does not disagree silently.
- **Tasks.** `dsh_tasks action=add` returns an id. Pass it as `task_id` to `dsh_task`. The server checks it before it sends anything. It refuses the dispatch if the id is unknown, closed, or owned by another worker. Then the server moves the task `dispatched → blocked → returned` as it observes the worker. `in-review`, `merged`, `rejected` and `abandoned` are the decisions of the orchestrator.
- **The digest is mechanical.** `dsh_digest` (or `dsh_wait … digest=true`, which attaches the digest when a turn ends) summarizes the last turns from the mirrored event log, with no model call. It lists:
  - the files edited (from `edit` and `write` calls)
  - the shell commands, by kind
  - the commands that ran as **checks**, each with ok or failed and the last output line
  - whether a check ran **after the last edit**
  - the errors and the tool-policy refusals
  - any goal that the worker created itself (`goalsCreatedByWorker`)
  - when the task has a worktree, what changed in git since the dispatch (the server records the state of the worktree at dispatch)

  A command counts as a check if it matches the acceptance line of the task (backticked commands), a built-in pattern for common runners, or a line in `<state dir>/checks.txt` (one regex for each line). The shell parsing is a heuristic. `python -c` and heredoc scripts show as `opaque`. A failing run behind `| tail` is recognised from its output, because the pipeline hides the exit status. The git section is the reliable record of what changed, whatever command did it.
- **Each dispatch records what it used.** With `task_id`, `dsh_task` stores the model, the reasoning effort and the permission of the worker on the task. The turn end stores the tokens, steps, wall minutes and context change of the dispatch, as dsh reports them. A field that dsh does not give is omitted, not estimated. `dsh_tasks show` adds a rollup. `dsh_tasks action=report` groups the tasks by model and effort, with the rounds for each task. It reports work, not money. Prices change, so look them up on the pricing page of the provider.
- **Writes answer briefly. `show` has everything.** The add and update actions of `dsh_tasks`, and the adopt, note and set actions of `dsh_roster`, return a short acknowledgement. For a task it has the id, status, owner, branch and note count. For a worker it has the role, the retired flag and the last note. It does not return the whole record. The caller just wrote the record, and the record with its history would sit in the context of the lead on every call. `dsh_tasks action=show` and `dsh_roster action=show session=…` return the full record.
- **Only managed sessions are mirrored.** If you inspect a session with `dsh_status`, that does not start recording it.
- **Cached state is "last observed".** Every snapshot carries `observedAt`. While dsh is up, the live tools are the source of truth.
- **Recording never changes a result.** If a call succeeded and writing the state then fails, the success stands. The server appends a `[state: …]` note. So the orchestrator never sends again work that already happened.

Measured: the first mirror of a session with 12,649 events (2,500 tool calls) takes 4.6 s and 24 MB. Later syncs of an unchanged session take 0.3 s. Keep `logs/` and `history/` out of version control. They hold the full transcripts of workers.

## Two facts to know before you build on this

**A blocked worker is indistinguishable from a finished one.** The `TurnEndReasonMap` of `dsh` is `completed | aborted | blocked | error | max-tokens | interrupted`. There is no input-required state. A worker that asked a question and waits ends its turn as `completed` and sits `idle`. The signal is on a different plane: an answerable server-request frame on the mux stream. `dsh_pending` reads it. Check it whenever a worker goes idle early.

**The event streams are WebSocket, not SSE.** `/api/events.mux` and `/api/events.host` answer a plain GET with `426 upgrade required`. The `dsh` source also contains an SSE carrier for the same API, but it belongs to a different deployment. That mistake cost a full working implementation that read zero frames.

The skill covers both facts in more detail, with source citations.

## Tests

```bash
uv run pytest            # or: PYTHONPATH=src python3 -m pytest
uv run ruff check src tests scripts
uv run mypy
uv run python scripts/complexity.py
```

CI runs these on every push and pull request, with the tests on Python 3.10, 3.11, 3.12 and 3.13. The last command is a complexity ratchet. `complexity-baseline.json` records each function with a cyclomatic complexity of 11 or more. A new function above that, or a rise in a recorded one, fails the check. After you lower one, run `uv run python scripts/complexity.py --update` and commit the file.

390 tests, no live server needed. They cover the places where a mistake is quiet and not loud:
- reasoning blocks that leak into replies (a real bug: reasoning blocks and answer blocks both carry a `text` field)
- turn-end reasons that nothing reports
- answer batching and its validation rules, which are not obvious
- an unreachable server that reports "nothing pending"
- a cancel that lost its race and reports success
- WebSocket framing
- the rule that no tool failure may escape `handle()` and kill the stdio transport

We exercised the original tools against a live `dsh` server. Verified behaviours worth knowing: the block and unblock loop works **unattended**, with no browser or UI client in the path. A title that you set explicitly **pins**, and it survives later turns. `dsh_cancel` tells a real abort from a lost race. We tested `dsh_digest`, `dsh_brief`, the usage record on tasks and the `compactionStalled` flag only against a fake server. Nobody has run them against a live `dsh` yet.

## Tested against dsh

The dsh API is not a documented, stable interface. This package uses it through three calls (`DshClient` in `transport.py`: `rpc`, `post`, `stream`). The package was tested against dsh package version `0.1.1-rc.2`, commit `b150a55`. `contracts/dsh-shapes.json` records the keys and types of real responses from that server. It holds no values.

dsh reports a version in `host.describe`, but it is the constant `0.0.1`, so the tool cannot use it. The tool checks the shape of responses instead. `shape.REQUIRED` lists the fields that the code reads. The server and `dshctl` check the first responses of each method. If a field is missing or has another type, the call still returns. `dsh_brief` then adds a `dshShape` list with the problem, and a line in `needsAction` that tells the lead to report it. `dshctl` prints a warning on stderr.

To record the shapes of another dsh, run `uv run python scripts/record-shapes.py` against it. The script makes read-only calls. A test checks that `shape.REQUIRED` fits the recording and fits the fake that the tests use. The recording does not cover `session.create`, `session.fork`, `session.rename` and `session.cancel`, because they change state. Those four are checked against the fake only. The frames of the pending-requests stream and the two POST endpoints (`respond`, `commands/execute`) have no shape check.

## Provenance

We checked every transport claim in this repo against a running server and the `dsh` source. We did not infer any of them from documentation. The code began as throwaway scripts in an evaluation workspace, and we extracted it from there.

## License

MIT
