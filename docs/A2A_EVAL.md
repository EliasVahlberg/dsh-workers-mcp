# A2A (Agent2Agent) vs. our dsh worker interface — evaluation

Evaluated 2026-09-20 against [A2A's specification](https://a2a-protocol.org/latest/specification/)
and [the a2aproject/A2A repo](https://github.com/a2aproject/A2A), prompted by the
question of whether A2A overlaps with `tools/dshctl/` + the `dsh-workers` MCP
server we built.

**Verdict: overlap is real but confined to one of the two layers we built, and
the divergence on the other layer is deliberate on A2A's part, not an omission.
Do not adopt A2A for local dsh workers. Steal three specific ideas from it.
Revisit if a worker ever leaves this machine.**

## What A2A is

Open protocol under the Linux Foundation, contributed by Google. Three
normative transport bindings (JSON-RPC 2.0, gRPC, HTTP+JSON/REST) that must be
functionally equivalent. Agent discovery via an **Agent Card** served at
`/.well-known/agent-card.json`, declaring name, version, skills, capabilities,
`supportedInterfaces`, and `securitySchemes`. Core data model is
`Task` (`id`, `contextId`, `status`, `artifacts`, `history`) with a
`TaskState` lifecycle, and `Message`/`Part` for content (text, files, structured
JSON). Supports synchronous request/response, SSE streaming, and asynchronous
push notifications to a webhook.

## dsh has no A2A support — verified

Grepped `reference/deepseek-harness/packages` for `a2a`, `agent2agent`, and
`agent-card.json` across `*.ts`/`*.md`/`*.json`, excluding `node_modules`:
**zero hits.**

What dsh *does* ship for out-of-process agent interop, from
`packages/subagent/`: `subagent-acp` (Zed's Agent Client Protocol — a different
standard, editor↔agent rather than agent↔agent), `subagent-claude-code`,
`subagent-codex`, `subagent-dsh-sdk`. So A2A would be new surface to build, not
a flag to enable.

## The overlap: delegation and lifecycle

| Our tool | A2A operation | Fidelity |
|---|---|---|
| `dsh_task` (async, returns immediately) | `SendMessage` with `returnImmediately: true` | near-identical |
| `dsh_ask` (blocks until turn end) | `SendMessage` with `returnImmediately: false` | near-identical |
| `dsh_cancel` | `CancelTask` | direct |
| `dsh_status` | `GetTask` (+ `historyLength`) | partial — see below |
| `dsh_list` | `ListTasks` (filter by `contextId`/status, paginated) | partial — ours lists sessions, theirs lists tasks |
| — | `SendStreamingMessage` / `SubscribeToTask` (SSE) | **we lack**; we poll instead |
| — | push-notification config CRUD (webhooks) | **we lack** |
| — | `GetExtendedAgentCard` | **we lack**; no discovery concept |

Worth noting that A2A's `returnImmediately` flag is the same async-vs-blocking
split as our `dsh_task`/`dsh_ask`, **and A2A's default is also blocking**. Two
independent designs landing on the same distinction with the same default is
mild evidence it's the natural decomposition rather than an arbitrary one.

## How much of A2A do we actually cover?

Counting A2A's core RPC surface, ignoring transport bindings:

| A2A capability | Us | Notes |
|---|---|---|
| `SendMessage` (blocking) | ✅ `dsh_ask` | same default |
| `SendMessage` (`returnImmediately`) | ✅ `dsh_task` | same split |
| `CancelTask` | ✅ `dsh_cancel` | ours additionally reports whether the abort *landed*; A2A returns the task |
| `GetTask` | ✅ `dsh_status` | ours is a superset — see the observability section |
| `ListTasks` | ⚠️ `dsh_list` | ours lists *sessions* (workers); A2A lists *tasks*. Coarser |
| `TaskState.INPUT_REQUIRED` | ⚠️ `dsh_pending` | function covered, not a typed state |
| `SendStreamingMessage` / `SubscribeToTask` | ❌ | we poll or block; the mux could back it |
| Push-notification config CRUD | ❌ | webhooks; no use for them on loopback |
| `GetAgentCard` / `GetExtendedAgentCard` | ❌ | no discovery concept; we know our workers |
| `Task.artifacts` | ❌ | deliberate — see below |
| `contextId` / `taskId` split | ❌ | conflated into "session"; the real gap |
| `referenceTaskIds` | ❌ | cross-informing is prose today |

**Roughly the delegation and lifecycle half, and none of the interop half.** Of
the three ❌ items that are not deliberate declines, only the `contextId`/`taskId`
split is a design gap we would actually feel; streaming and artifacts are
answered differently rather than missed (see below), and Agent Cards solve
discovery, a problem we do not have with four named local workers.

This is the expected shape. The overlap is exactly where both designs answer
"how do I hand work to another agent and find out how it went", and the
divergence is exactly where A2A's opacity principle bites.

### On artifacts specifically

A2A returns `Task.artifacts` — structured, typed outputs. We return the worker's
reply text and nothing else, which sounds like a gap and mostly is not: our
workers produce *commits in a worktree*, and the artifact is the diff. Git is the
result channel, and it is a better one than a typed blob because it is
independently verifiable after the fact by a party that never saw the task. The
place this would bite is a worker whose output is genuinely a value rather than a
change to a tree — none exist yet.

## The non-overlap: observability, and why it is structural

A2A's stated guiding principle is *"Opaque Execution: Agents collaborate ...
without needing to share their internal thoughts, plans, or tool
implementations."* The repo README states the same goal twice, including
*"Preserve Opacity: Allow agents to collaborate without needing to share
internal memory, proprietary logic, or specific tool implementations."*

Our interface is built on the exact inverse premise. These have no A2A
counterpart and never will, because exposing them is what A2A is designed to
avoid:

- `dsh_grep` — regex over one worker's own full event log, with paging back
  toward session start
- `dsh_search` — full-text across *all* sessions, for attribution ("which
  worker discussed X")
- `dsh_models` — provider/model/`reasoningEffort` introspection and control
- from `dsh_status`: `contextPressure` and per-category context breakdown,
  token usage with cache-hit percent, turn/step counts, LLM-vs-tool minutes,
  `reasoningChars`

A2A gives a client `Task.history` (messages) and `artifacts`. It will not tell
you a worker sits at 39.5% context pressure with a 99.7% cache-hit rate, nor
that it withheld 549 characters of reasoning — all things we measured and use.

Likewise A2A has no counterpart to `session.fork`, agent-preset selection, or
reasoning-effort control, because those are internals by its definition.

**The one-line framing:** A2A solves cross-organisation interop between agents
you do not control. We solved close supervision of local workers we
deliberately want to see inside of. Same JSON-RPC-shaped plumbing, opposite
premises about how much the caller is entitled to know.

## Three ideas worth taking

### 1. Split `contextId` from `taskId`

A2A separates the ongoing conversation (`contextId`) from an individual unit of
delegated work (`Task.id`), so many tasks can share one context. We conflate
both into "session": one dsh session is simultaneously the teammate and every
piece of work it has ever done.

Consequence today: we can ask *"is this worker busy?"* but not *"which
delegated tasks are still outstanding for this worker, and which completed?"*
A dsh turn is the closest thing to an A2A task, and turns are only addressable
by seq, not by a handle we chose at dispatch time. Adopting the split would
mean `dsh_task` returning a task handle rather than just accepting the dispatch.

### 2. `TaskState.INPUT_REQUIRED` — now covered at the interface, still absent from the type

**Superseded in part.** This section originally recorded input-required as a flat
gap on the grounds that a blocked worker and a finished one both surface as
`idle`. That observation is correct about the *turn-end reason* and wrong about
the *interface*: dsh does ship a full structured question mechanism, and
`dsh_pending` reads it. The two states are now distinguishable — a blocked worker
appears in `dsh_pending` with its `rpcId` and question, a finished one does not.
`dsh_answer` settles it. Verified end to end, unattended, with no UI client.

So the functional equivalent of `INPUT_REQUIRED` is covered. What remains absent
is the *typed state*: there is no `input-required` variant in `TurnEndReasonMap`,
so the fact must be discovered by a second call rather than read off the turn's
ending. A2A's shape is still better here — one field instead of a join.

Verified against `packages/core/session/src/types.ts:155-177`. dsh's complete
`TurnEndReasonMap` is:

| kind | meaning |
|---|---|
| `completed` | normal finish |
| `aborted` | cancellation interrupted the live turn (cause: `user`/`parent`/`hook`/`disposed`/`legacy`) |
| `blocked` | see below |
| `error` | structured `LlmFailure` |
| `max-tokens` | a step hit its output-token ceiling |
| `interrupted` | a crash-orphaned turn closed by persistence on reload; never emitted by the loop |

No input-required analog. I checked whether `blocked` (undocumented in the
type) was one: it is not. It comes from `packages/core/agent-loop/src/agent.ts:268`,
set when `preStep` returns `decision.kind === 'reject'` — a gate refusing to
*start* the turn, not a pause pending input. `packages/acp/acp/src/codec.ts:27`
maps it to ACP's `end_turn` alongside `error`.

The actionable part: the source comments call `TurnEndReasonMap` a
*"merge-extensible sum type"* that *"plugins extend ... by merging variants into
the map."* So an input-required state is addable by a Cordis plugin rather than
requiring an upstream change — which is squarely the kind of customization this
repo exists to build.

### 3. `referenceTaskIds` on a Message

A2A puts an explicit, typed slot on a message for "this work relates to those
other tasks." That is exactly the cross-informing mechanism we need when worker
A's changes affect worker B, and which we would otherwise hand-roll as prose
inside a prompt. Worth mirroring in whatever the orchestrator relays between
workers, even without adopting the rest of A2A.

## A framing wrinkle: we exposed agents as tools

A2A's [Appendix B](https://a2a-protocol.org/latest/specification/#appendix-b-relationship-to-mcp-model-context-protocol)
positions MCP as agent-to-*tool* and A2A as agent-to-*agent*, and the project
describes the two as complementary rather than competing.

By that taxonomy we built an A2A-shaped capability on MCP-shaped transport: the
workers are agents, but they are presented to the orchestrator as tools it
calls. Pragmatically fine for one orchestrator driving local workers — it is
simpler and it works. It stops being fine the moment a worker needs to
*initiate* contact rather than only respond, since a tool has no way to call
back. A2A's push notifications and client-initiated-method roadmap item exist
because that limit is real.

## When to revisit

Adopt A2A if any of these become true:

- a worker runs on a different machine, or under a different framework/vendor
- workers must interoperate with agents we did not configure
- we need agent *discovery* rather than reading `settings.yaml` ourselves
- the unauthenticated-loopback posture stops being acceptable — A2A's
  `securitySchemes` and Agent Card auth are the ready-made answer, and our
  current transport trusts any local process with full tool access

Until then, adopting it would cost the observability layer that makes the
current setup useful, in exchange for interop we do not need.

## Confidence and provenance

- **Verified by reading source in this repo:** absence of A2A in dsh; the
  `subagent-*` provider list; the complete `TurnEndReasonMap`; the origin and
  meaning of `blocked`; its ACP stop-reason mapping.
- **Verified by fetching A2A's own published docs:** the operation list,
  transport bindings, `Task`/`TaskState`/`Message`/`Part` model, Agent Card
  discovery path, the opacity principle (quoted), the MCP-relationship framing.
- **Not verified:** nothing was built or run against A2A. No `a2a-sdk` was
  installed, no Agent Card served, no interop tested. The mapping table above
  is a reading of both specifications, not a measured integration. The claim
  that a plugin could add an input-required turn-end reason follows from the
  type's documented extensibility and has **not** been implemented or booted.
