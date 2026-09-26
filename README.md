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
      "args": ["--directory", "/abs/path/to/dsh-workers-mcp", "run", "dsh-workers-mcp"],
      "timeout": 600000
    }
  }
}
```

Use an absolute path. A relative one resolves against the agent's working
directory, which is the whole reason this lives in its own repo.

The companion skill at [`skills/dsh-workers/SKILL.md`](skills/dsh-workers/SKILL.md)
documents the protocol, the operational traps, and compaction semantics. For
kiro-cli, reference it as `skill:///abs/path/to/dsh-workers-mcp/skills/dsh-workers/SKILL.md`.

## The tools

| Tool | Does | Blocks? |
|---|---|---|
| `dsh_list` | roster: who exists, who is busy, context pressure | no |
| `dsh_status` | deep insight into one worker, incl. `lastTurnEnd` | no |
| `dsh_grep` | regex over one worker's own history, paged backwards | no |
| `dsh_search` | full-text across every session, for attribution | no |
| `dsh_models` | model, and reasoning-effort levels available | no |
| `dsh_pending` | **what a worker is blocked on** | no |
| `dsh_answer` | **unblock it** | no |
| `dsh_task` | dispatch real work | no |
| `dsh_ask` | quick question, wait for the reply | yes |
| `dsh_wait` | wait for a running worker's turn to end | yes |
| `dsh_goal` | read/revise the standing objective | no |
| `dsh_create` | create a worker | no |
| `dsh_cancel` | cancel the running turn | no |

Read-only tools are safe to auto-approve. `dsh_task`, `dsh_ask`, `dsh_answer`,
`dsh_goal`, `dsh_create`, and `dsh_cancel` change worker state — gate them
deliberately, since a worker acts on an answer with full tool access.

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

73 tests, no live server needed. They cover the places where being wrong is
quiet rather than loud: reasoning blocks leaking into replies (a real bug —
reasoning and answer blocks both carry a `text` field), turn-end reasons being
swallowed, answer batching, WebSocket framing, and the rule that no tool failure
may escape `handle()` and kill the stdio transport.

## Provenance

Every transport claim here was verified against a running server and the `dsh`
source, not inferred from documentation. Extracted from
`deepseek-harness-eval`, where it began as throwaway scripts.

## License

MIT
