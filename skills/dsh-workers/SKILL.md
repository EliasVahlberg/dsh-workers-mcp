---
name: dsh-workers
description: Drive dsh worker agent sessions from this orchestrator — observe their stats and context pressure, search their history, delegate long tasks, and hold short exchanges. Use when delegating work to a dsh agent, checking on a running worker, or asking a worker for an opinion.
---

# Driving dsh worker sessions

The orchestrator (this agent) holds the cross-session view; `dsh` sessions are
long-lived teammates that do the work. Interaction goes through the
**already-running `dsh web` server**, not a separately owned process — so
worker sessions stay visible in the browser UI and inherit
`$DSH_HOME/settings.yaml` (model wiring, vision support) by construction.

Tools come from the `dsh-workers` MCP server (`tools/dshctl/mcp_server.py`).
The same operations are available as a CLI for ad-hoc use:

```bash
python3 tools/dshctl/dshctl.py list
python3 tools/dshctl/dshctl.py status <session>
python3 tools/dshctl/dshctl.py ask <session> "question"
```

## The operations

| Need | Tool | Blocking? |
|---|---|---|
| Who is on the team, who is busy | `dsh_list` | no |
| Deep insight into one worker | `dsh_status` | no |
| Search one worker's own history | `dsh_grep` | no |
| Find which worker discussed X | `dsh_search` | no |
| Which model a worker runs on | `dsh_models` | no |
| Delegate real work | `dsh_task` | **no** — returns immediately |
| Quick question / design opinion | `dsh_ask` | yes, until turn end |
| Wait for a running worker | `dsh_wait` | yes |
| **What a worker is blocked on** | `dsh_pending` | no |
| **Unblock it** | `dsh_answer` | no |
| Read/revise its standing objective | `dsh_goal` | no |
| **Name a worker by its role** | `dsh_name` | no |
| Hire a new worker | `dsh_create` | no |
| Interrupt a running turn | `dsh_cancel` | briefly, to confirm the abort |

The three groups matter more than the individual tools: **delegate**
(`dsh_task`/`dsh_ask`), **report** (`dsh_status`/`dsh_grep`), and **unblock**
(`dsh_pending`/`dsh_answer`). The rest is bookkeeping that keeps a long-lived
roster legible.

Delegation pattern: `dsh_task` to dispatch, then `dsh_status` to poll
`state` until it flips `running` → `idle`, then read `latestReply`. The
orchestrator is never blocked while a worker runs.

Prefer `dsh_wait` over a polling loop when you have nothing else to do. Polling
spends an orchestrator turn per check, and orchestrator turns are the expensive
resource; blocking spends only wall-clock. `dsh_wait` with no explicit `after`
waits for a *running* worker's next turn end and returns an *idle* worker's last
one immediately, so it is safe to call either way.

`mode` on `dsh_task`/`dsh_ask` is real mailbox semantics: `queue` appends to the
worker's inbox and waits for its current turn to finish; `steer` interrupts the
running turn to redirect it. Default `queue`.

## A blocked worker looks exactly like a finished one

This is the single most important operational fact in this file.

dsh has **no input-required turn state.** `TurnEndReasonMap`
(`packages/core/session/src/types.ts`) is exactly `completed | aborted |
blocked | error | max-tokens | interrupted`. A worker that asked a question and
is waiting ends its turn `completed` and sits `idle` — byte-identical in
`dsh_list`/`dsh_status` to one that finished its task. `blocked` is *not* that
state either: it comes from `preStep` returning `reject`
(`packages/core/agent-loop/src/agent.ts:268`), a gate refusing to *start* a
turn, and ACP maps it to `end_turn` alongside `error`.

The real signal lives on a different plane — an answerable **server-request**
frame on the mux downlink. `dsh_pending` is the only way to see it. Check it
whenever a worker goes idle sooner than the work should have taken.

Answering is a batch operation: one `ask()` may carry several questions and
takes **one** answer covering all of them. Never answer them in separate
`dsh_answer` calls.

### The answer rules, because a violation is silent

The host validates a batch in `matchesQuestions`
(`packages/host/apiproxy/src/api-proxy.ts`) and rejects it with a bare
`{accepted: false, reason: "bad-response"}` — no indication of which rule broke,
and the worker stays blocked while the call looks like it did something.
`dsh_answer` checks these locally first and names the failure, but know them:

- **Answer every question in the ask, in one batch.** A partial batch is rejected.
- **Order matters.** The host zips answers to questions by index *then* checks
  the id, so a correct set in the wrong order fails.
- **Labels are verbatim.** Case and whitespace included; only offered labels.
- **No duplicate labels** within one answer.
- **`custom` must not be blank** — omit the field rather than sending `""`.
- **On a single-select question, `custom` and `selected` are mutually
  exclusive**, and `selected` takes at most one label. Multi-select allows both
  together and several labels.

The last one is the trap. Sending a chosen option *and* an explanatory note is
the natural thing to do and is rejected; put the note in the follow-up prompt
instead.

### It works unattended — verified

A pending question is only visible for an **attached** (in-memory) session, and
a freshly started server with no browser open has none. That is correct rather
than limiting: a cold session is not running, so it cannot have a question
outstanding. Dispatching with `dsh_task` attaches the session itself.

Verified on a fresh server with no browser: baseline was 0 frames, `dsh_task`
then produced a worker question that `dsh_pending` saw and `dsh_answer` settled.
No UI client is required anywhere in the loop.

Verified end to end: worker called `ask_user_question` → `dsh_pending` returned
its `rpcId`, question id, header, and both option labels with descriptions →
`dsh_answer` returned `{"accepted": true}` → the question left `pending` and the
worker continued, confirming the selection it had been given.

`dsh_pending` also surfaces `approval/requested` frames — a worker stopped on a
tool approval. Nothing in this interface can *grant* one, so an unattended
worker that hits an approval gate stays stopped until a human acts. Treat a
persistent `approval/requested` as a stall that needs a person, not a bug.

## Name your workers, immediately

An unnamed roster is unusable. Titles are auto-generated from the conversation,
and for a long-lived worker the conversation opens with orientation boilerplate,
so the generated title describes the *setup*, not the worker. Measured on a real
roster: the main working session was titled `## Orientation Read AGENTS.md
first` — a fragment of its own prompt markdown — and two others were titled with
truncated first-prompt text. Four sessions on one project, none distinguishable.

`dsh_name` fixes that, and the name **pins**: automatic regeneration is
superseded for the life of the session (verified — a title survived a subsequent
turn unchanged). That is what makes it an identifier rather than a description.

Name by **standing role, not current task** — the session outlives the task:

- good: `implementer — machine entities`, `reviewer — takes positions`,
  `sandbox — safe to break`
- bad: `fixing the belt test`, `v0.6 fuel bands` (stale within the hour)

Name a worker when you create it, before dispatching anything. Titles are
collapsed to one line and capped at 80 UTF-8 bytes; `dsh_name` echoes the
accepted string and flags it when normalisation changed what you sent.

## Cancelling: the receipt is not evidence

`session.cancel` returns `{accepted: true}` unconditionally — byte-identical for
an idle session and a mid-flight one. Trusting it means believing you stopped
work that may have already finished and committed its side effects.

`dsh_cancel` therefore reports `cancelled` from the turn's actual ending: a real
interruption lands `turn/end` with kind `aborted`, reason `user`. If the turn
ended as `completed` instead, the cancel lost the race and **that turn's work
stands** — check the worktree before assuming otherwise.

## Turn-end reason, and why polling `state` is not enough

`dsh_status` reports `lastTurnEnd: {seq, kind, ...}`, and `dsh_wait`/`dsh_ask`
return the same. Read it before trusting that delegated work succeeded: a turn
that died on `error`, hit `max-tokens`, or was `aborted` leaves the session
`idle` exactly like a clean `completed` one. `aborted` carries its cause
(`user`/`parent`/`hook`/`disposed`/`legacy`) and `error` carries a structured
`LlmFailure`; both are passed through verbatim.

`latestReply.text` can be **empty while `reasoningChars` is large** — a turn
whose committed message held only reasoning blocks. Measured on a real 57-turn
session: empty text, 878 characters of reasoning. Do not treat an empty reply as
"the worker said nothing"; check `lastTurnEnd` and `dsh_pending` before
concluding anything.

## Goals: the orientation that survives compaction

`dsh_goal` reads and revises a session's standing objective. This matters
because a summary is lossy prose, whereas the goal is **structured state** —
making it the durable home for what a long-lived worker is for. `show` reads it
(from the `goal` session projection, the only place current state lives); `set`
creates; `edit` revises objective and/or round cap; `pause`/`resume`/`complete`/
`clear` move its phase.

Mutations carry the current `{id, revision}` ref, so a concurrent edit is
rejected rather than silently overwritten — re-read before editing.

## Transport, if you need to go below the tools

`POST http://127.0.0.1:3080/api/<method>` with

```json
{"type":"client-request","rpcId":"<any-string>","method":"<same-method>","payload":{}}
```

Response: `{"type":"server-response","rpcId":...,"result":{"ok":true,"value":{...}}}`.

This is **not JSON-RPC 2.0** — no `jsonrpc` field, `rpcId` rather than `id`, and
the method appears in the URL path as well as the body. A single `/api/rpc`
endpoint does not exist; `/rpc` returns 405, `/api` alone returns 404.

Loopback requests are trusted with **no credentials** — any local process can
create and drive agent sessions with full tool access. Worth knowing before
leaving the server running. There is a browser-trust fence
(`isTrustedApiRequest`, a DNS-rebinding defence) and privileged methods are
pinned to loopback, but sending no `Origin` header passes.

### The two event streams are WebSockets, not SSE

Unary calls are the POST above, but the two event streams
(`/api/events.mux`, `/api/events.host`) are served by the running `dsh web`
server as **WebSocket upgrades**. A plain `GET` answers `426 upgrade required`
(`packages/client/connection/src/index.ts`). `packages/host/apiproxy/src/fetch/handler.ts`
*does* contain an SSE carrier for the same API — it is a different deployment,
and assuming it cost a full implementation that read zero frames before `curl`
showed the 426. Check the carrier, do not infer it from the contract.

Each WebSocket text message is `JSON.stringify(ServerRequest)`, i.e.
`{type, rpcId, method: <frame.type>, payload: <frame>}` — so **the rpcId to echo
when answering is on the envelope, not inside the payload**. Per
`websocket-downlink.ts`, "client messages are a protocol violation: upstream
traffic remains on HTTP", so a read-only client is sufficient and answers go
back over `POST /api/respond`. Server-to-client frames are never masked, which
is why `dshctl` carries a ~60-line stdlib WebSocket reader rather than a
dependency.

The detail that makes a discrete-call tool workable: the mux stream **replays
every still-pending approval and question frame on open**, reusing each rpcId
verbatim — the "refresh-recovery baseline". A short-lived connection is
therefore a complete snapshot of outstanding work, so nothing needs to hold a
socket open to notice a blocked worker.

Full method map lives in
`reference/deepseek-harness/packages/host/apiproxy/src/api/rpc-map.ts`
(~40 methods: `session.*`, `subagent.*`, `workspace.*`, `agentPreset.*`,
`goal.*`, `skill.*`, `host.*`).

## Gotchas that cost real time

**Reasoning blocks carry a `text` field too.** An `assistant/message`'s content
can hold both a `{type:'reasoning', text:...}` block and a `{type:'text',
text:...}` block. Any extractor that takes "every block with a `text` field"
silently concatenates the model's private thinking onto its answer — which
reads exactly like the model rambling, and was misdiagnosed once as a
qwen-specific defect before the block types were actually inspected.
`dshctl` excludes `reasoning`/`thinking` blocks and reports `reasoningChars`
so you can see how much was withheld; `status --reasoning` shows it.

**`session.history`'s `maxMessages` counts messages, not events.** Measured: 3
messages returned **1437 events**, 1424 of them `assistant/chunk` streaming
deltas. Never hand raw history to a model's context. `dshctl` filters
`assistant/chunk`/`reasoning/chunk` everywhere; keep it that way. The events
that matter are `assistant/message` (committed text plus usage), `tool/call`,
`tool/result`, and `turn/end` (carries the finish reason).

**`session.create` has every field optional.** An empty payload silently
succeeds and creates a blank session. `dsh_create` refuses a payload with
neither `cwd` nor `preset` for exactly this reason. (This mistake was made
once already, probing for a validation error.)

**`workspace.archiveSession` does not remove a session from `session.list`.**
It returns `ok` with the archived id, but the session still appears, marked
`blank: true`.

**Detect turn completion by `turn/end`, not only by the `running` flag.** There
is a window right after prompting where `running` is still `false` because the
turn has not started; polling the flag alone races. `dshctl` records the head
`asOfSeq` before prompting and waits for a `turn/end` with a greater seq.

**`session.grep` only sees a recent window by default.** One page of 200
messages is ~1000 non-chunk events; a long session's seqs run past 500,000.
Raise `pages` to walk further back, and check `reachedSessionStart` in the
result before concluding something is absent.

**`session.selectModel` rewrites the GLOBAL default.** Pinning a model on one
session persists that choice to `agent-default-model` in
`$DSH_HOME/settings.yaml`, so creating a single qwen-pinned worker silently made
qwen the default for every future session. The per-session pin and the global
default are independent *after* the fact, so restoring the file fixes new
sessions without disturbing the pinned worker. Re-read that block after pinning
anything — otherwise a later "why is this worker on the wrong model?" is very
hard to trace.

**`session.search` is opt-in and ships disabled.** Both the base and web-app
bundles set the session-query index to `openAt: never`, so the RPC fails with
`SESSION_QUERY_SEARCH_DISABLED`. It is enabled here by a `session-query-sqlite`
override in `$DSH_HOME/cordis.patch.yml` setting `openAt: first-search` — the
path the web-app bundle's own comment names as intended. Two things that matter
when writing that override: a non-insert patch **replaces the entire `config`
object** rather than merging into it, so `path: ':memory:'` has to be restated
or it reverts to the schema default; and the change took effect on the
already-running server without a restart, same as `settings.yaml`.

**`session.models` returns `current` and `groups`,** not `selected` and
`providers`. `deepseek-flash` exposes `reasoning.efforts`
(`off`/`low`/`high`/`max`, default `high`) — dropping effort is a real cost and
latency lever for cheap short exchanges.

## Compaction: not a degradation to defend against

Verified against `docs-kb/full-tree/docs/subsystems/compaction.md` and measured
on a real 39-turn session:

* Compaction **shadows, it does not delete**. The summary rides on a
  `user/message` with `surfaceOp: {op:'replace', start, end}`; the original
  events stay in the append-only log with their seqs recorded in
  `shadowedSeqs`. Only surface events reach the model.
* **System-prompt steering survives.** Prompt-context contributions are
  re-asserted when compaction removes them, so `AGENTS.md`-level steering
  cannot erode through compaction.
* `compactNow()` runs as idle maintenance and **returns null without writing
  when no useful range exists**. In the measured session, 29 `start`/`end`
  brackets produced only **15** real summaries — the other 14 were no-ops.
  Count summaries, not brackets.
* Measured effect on behaviour across those cycles: read:edit ratio flat
  (0.44 → 0.40 → 0.41), error rate *improved* (6.6% → 3.8%), cache hit rate
  still 97%. Long-lived sessions are the right default.

**The real risk is an orphaned lock, not quality.** A crash inside a compaction
bracket leaves a `compaction/start` with no matching `compaction/end`, and a
live unmatched start blocks every compaction entry point thereafter. The
symptom is silent: `contextPressure` climbing with no new summaries. Watch
`context.usedPct` in `dsh_status`.

Because summaries are lossy by design, **decisions must land in the repo**, not
only in a worker's context. Facts can be re-derived by re-reading files;
rejected alternatives and their rationale cannot. Treat "did the worker commit
its decisions to the docs?" as a gate before a long session rolls on.

## Steering workers

Measured across two full sessions: mechanically checkable instructions get
followed, preferences do not. One worker ran `make test`/`pytest` 150 times
(every commit green) while using the tool its steering emphasised most in only
12 of 2517 calls. So hand workers **gates** — "this suite must pass", "this
guard must reject these N inputs", "read before editing" — rather than habits.

Keep verification orchestrator-side: the worker builds, the orchestrator
independently re-runs the check. Do not accept a transcript as evidence.

## The two workers

| | deepseek worker | qwen worker |
|---|---|---|
| provider/model | `deepseek-official` / `deepseek-flash` | `qwen-cloud` / `qwen3.8-flash` |
| context window | 1,000,000 | 1,000,000 |
| max output | 384K (adapter default caps at 256K) | 131,072, verified first-party |
| reasoning control | `reasoning.efforts` off/low/high/max, default high | none exposed |
| measured latency, trivial question | ~3 s | ~6 s |
| measured decode throughput | 259 tok/s | 61 tok/s |

Create either with `dshctl create --cwd <path> --preset <name> --provider <p>
--model <m>`; omitting provider/model inherits `agent-default-model`.

**A model swap alone does not make a worker feel distinct.** Given the same
preset and the same prompt, both models answered an open design question with
the same recommendation and the same primary risk. Differentiation has to come
from the preset/persona and the kind of work each is handed, not from the model
id. Treat divergence as something to engineer and then verify, not as a free
property of using two vendors.

**Model config lives in `$DSH_HOME/settings.yaml` and is picked up by the
running server without a restart** — verified by editing `contextWindow` and
seeing a newly created session report the new value. An `llm-pi-ai` model entry
with no `contextWindow` silently inherits `defaultContextWindow`, documented as
262,144 and "a guess by construction"; that guess made a real session compact
about four times more often than necessary.

**But an existing session keeps the window it was created with.** The 57-turn
qwen worker still reports `window: 262144` after `settings.yaml` was corrected
to 1,000,000 — only sessions created afterwards see the new value. So the
longest-lived, best-oriented worker is the one still paying the old compaction
rate, which is exactly backwards from what you want. Check `context.window` in
`dsh_status` against the configured value before assuming a fix applied.
