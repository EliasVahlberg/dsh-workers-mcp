# Suggestions from comparable frameworks (30 Sep 2026)

Why this exists: the orchestrator was built for long-horizon work, but its
sessions had drifted into quick tasks, each ending with a report and a wait for
the user. This compares that setup against other multi-agent orchestrators and
lists what to adopt, in order.

Sources read (READMEs, and OpenRig's docs in full where relevant):

- [OpenRig (mvschwarz)](https://github.com/mvschwarz/openrig): a persistent,
  YAML-defined agent team with a lead that coordinates specialists. The closest
  match; most of the items below come from its `docs/reference/`
  (`wave-sdlc.md`, `planning-dial.md`, `refocus-channel.md`,
  `release-boundary.md`, `knowledge-maturity.md`).
- [awesome-agent-orchestrators](https://github.com/andyrewlee/awesome-agent-orchestrators):
  its "autonomous loop runners" and "task runners" sections (ralphex,
  bernstein, paperclip).
- [OpenRig (EliasOenal)](https://github.com/EliasOenal/OpenRig): a multi-user,
  sandboxed agent runtime. Relevant only for its scheduled (cron) runs.
- [OpenWork](https://github.com/different-ai/openwork): a desktop app for
  sharing skills and MCP servers. Nothing to adopt beyond per-worktree dev
  profiles, which the headless harness already has.

## Finding

The pull toward short tasks is partly written into the method, not only a
habit:

- nothing can wake the lead, so every task boundary is also a boundary for the
  user;
- the task-sizing rule (PLAYBOOKS §2, "fits in one worker turn, roughly under
  two hours") and the expected-size line in `DISPATCH.md` both push toward
  small tasks;
- every task ends in a verdict, and each verdict is a place where the turn
  stops.

## Suggestions, in the order to do them

### 1. Make the whole spec the default scope (doc change)

OpenRig's wave model makes the whole spec the default scope for a builder,
"built in one sustained run". Handing out anything smaller has to name its
reason on the brief (an open decision, a territory conflict, a real fork in the
road), and "an unexplained small chunk is a dispatch defect". The builder
chooses its own build order: step-by-step instructions from the orchestrator
are a cage, not diligence.

Change:

- PLAYBOOKS §2: the default task is a whole feature or phase, possibly several
  worker turns, one green commit per step. Splitting it needs a stated reason.
- The expected-size line in `DISPATCH.md` stays, but it describes the whole
  spec. It is not a cap to cut against: it still catches a retune that comes
  back at 750 lines.

### 2. Choose planning rigor per piece (doc change)

OpenRig's "planning dial" chooses rigor per piece, from P0 to P4:

| Rung | What the piece gets | When |
|---|---|---|
| P0 | a few outcomes and pointers | simple, reversible, well-trodden work |
| P1 | a written spec with a proof contract (default) | most work |
| P2 | P1 plus a research round before the spec freezes | the spec would freeze on an open unknown |
| P3 | P2 plus an adversarial read by a non-author | a wrong plan would look right from inside |
| P4 | P3 plus a blind design written first, then compared | rarely |

The test for going higher is "would a wrong plan be expensive, hard to undo and
invisible from inside?", not "is this piece important?".

Change: add the dial to PLAYBOOKS §2. It generalises the rule "size acceptance
to the change".

### 3. Review per batch, not per task (method change)

OpenRig merges each piece serially as soon as it is green with its own checks,
then reviews once per batch ("wave") across the whole merged range.

- The batch gets two reviewers who did not write the code, asking different
  questions: does the structure hold, and does each claim survive a check
  against the source.
- They also review for **drift**: is the result still what was asked for, or
  did reasonable local steps add up to something larger?
- Each miss is labelled a **context gap** (the spec lacked it, so the fix goes
  into the spec) or a **judgment gap** (the builder had the context and chose
  wrong, so the fix is a check). The count of each shows whether to improve
  specs or checks.

Change:

- PLAYBOOKS §6: the lead checks scope and the suite for each task, then
  merges.
- The full review (the reviewer worker plus the lead) runs per milestone
  phase, not per task.
- Add drift and the gap label to `templates/REVIEW.md`.
- The existing reviewer on a different model already fits the "different
  vantage" advice.

### 4. Sweep durable state on every wake (small script change)

OpenRig: "wakes are unreliable; the sweep is the read". On each wake, a seat
re-reads its open queue instead of trusting what it remembers.

Change: `lead-brief.sh` (a project tool) also lists tasks that are `returned`
and waiting for a verdict, workers that are idle with no task, and anything in
`dsh_pending`. This is the first thing a woken lead needs.

### 5. Re-deliver orientation after compaction (investigate first)

OpenRig's refocus hook re-sends the doctrine files after compaction or heavy
transcript growth, because a running session never re-reads files from disk.
A lead running for hours needs the current milestone and the method again after
it compacts.

Change: check whether a kiro agent hook can fire after compaction. If one can,
it prints the milestone file and the METHOD summary. If none can, the driver
in suggestion 6 includes them in its resume prompt.

### 6. A driver that wakes the lead (needs a decision first)

In OpenRig the lead is a live terminal session: a worker's `rig send` types
into it, so a finished worker wakes the lead. Paperclip uses heartbeats, and
EliasOenal/OpenRig uses cron jobs. The lead here runs only when the user types.

Change: add a thin script, not a service. It waits on dsh until a worker goes
idle or blocked, or a timer fires. Then it runs:

`kiro-cli chat --no-interactive --resume-id <lead> --agent <lead-agent> "<continue the milestone>"`

It stops when the milestone file says done or blocked, or when a budget (hours
or turns) is spent.

Prerequisites and unknowns:

- The `--no-interactive` and `--resume-id` flags exist (checked in
  `kiro-cli chat --help`).
- It is not yet tested how a non-interactive run handles a tool call that isn't
  pre-approved (it probably refuses it). So the lead's command allow-list must
  first cover `git` and `make` in the project repos. That widening is the
  user's decision.

## Already covered here

- Telling "blocked" from "finished" (`dsh_pending`; OpenRig treats needs-input
  as a count with a reason, the same idea).
- A server-side refusal of full access, not only a prompt rule.
- Forking to recover from content-filter poisoning (`dsh_fork`).
- Test isolation per checkout (the same idea as OpenWork's `dev:worktree`).
- Improving the framework from friction entries. OpenRig's knowledge ladder
  (data, field notes, insights, canon) has the same shape as
  `FRICTION.md` → METHOD Lessons → PLAYBOOKS.

## Not worth copying

- OpenRig's ceremony: lock stamps, receipts on every candidate, `rig mode`
  scope bindings, multi-phase migration receipts. It is hard to maintain and
  test at this scale, and it solves problems a two-worker team does not have.
- EliasOenal/OpenRig's OS-level user isolation: bwrap plus the permission
  presets already cover a single-user machine.
- OpenWork's capability marketplace: not an orchestration problem.
