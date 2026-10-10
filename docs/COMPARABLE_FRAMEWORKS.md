# What comparable frameworks suggested, and what became of it

Research notes, written 30 Sep 2026. The method had been built for long-horizon
work, but in use its sessions drifted into quick tasks, each ending with a
report and a wait for the user. This compares it against other multi-agent
orchestrators and lists what to adopt. Four of the six suggestions are in the
method now; two are open as issues.

| # | Suggestion | Status |
|---|---|---|
| 1 | The whole spec is the default scope | done: PLAYBOOKS §2 |
| 2 | Choose planning rigor per piece | done: PLAYBOOKS §2 (the P0-P3 dial) |
| 3 | Review per batch, not per task | done: PLAYBOOKS §7, `templates/REVIEW.md` |
| 4 | Sweep durable state on every wake | done: `dsh_brief` |
| 5 | Re-deliver orientation after compaction | open: issue #12 |
| 6 | A driver that wakes the lead | open: issue #11 |

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

The pull toward short tasks was partly written into the method, not only a
habit:

- nothing can wake the lead, so every task boundary is also a boundary for the
  user;
- the task-sizing rule ("fits in one worker turn, roughly under two hours") and
  the expected-size line in `DISPATCH.md` both pushed toward small tasks;
- every task ends in a verdict, and each verdict is a place where the turn
  stops.

## The suggestions

### 1. Make the whole spec the default scope (done)

OpenRig's wave model makes the whole spec the default scope for a builder,
"built in one sustained run". Handing out anything smaller has to name its
reason on the brief (an open decision, a territory conflict, a real fork in the
road), and "an unexplained small chunk is a dispatch defect". The builder
chooses its own build order: step-by-step instructions from the orchestrator
are a cage, not diligence.

Adopted: the default task is a whole feature or phase, possibly several worker
turns, one green commit per step; splitting it needs a stated reason. The
expected-size line in `DISPATCH.md` describes the whole spec. It is not a cap
to cut against, but it still catches a small change that comes back at 750
lines.

### 2. Choose planning rigor per piece (done)

OpenRig's "planning dial" chooses rigor per piece, from P0 to P4:

| Rung | What the piece gets | When |
|---|---|---|
| P0 | a few outcomes and pointers | simple, reversible, well-trodden work |
| P1 | a written spec with a proof contract (default) | most work |
| P2 | P1 plus a research round before the spec freezes | the spec would freeze on an open unknown |
| P3 | P2 plus an adversarial read by a non-author | a wrong plan would look right from inside |
| P4 | P3 plus a blind design written first, then compared | rarely |

The test for going higher is "would a wrong plan be expensive, hard to undo and
invisible from inside?", not "is this piece important?". Adopted as P0-P3 in
PLAYBOOKS §2; it generalises the rule "size acceptance to the change".

### 3. Review per batch, not per task (done)

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

Adopted: the lead checks scope and the suite for each task and merges; the full
review runs per milestone phase. Drift and the gap labels are in PLAYBOOKS §7
and `templates/REVIEW.md`.

### 4. Sweep durable state on every wake (done)

OpenRig: "wakes are unreliable; the sweep is the read". On each wake, a seat
re-reads its open queue instead of trusting what it remembers. Adopted as
`dsh_brief`: tasks waiting for a verdict, idle workers with no task, anything in
`dsh_pending`, stalled compactions. It is the first call of every lead session.

### 5. Re-deliver orientation after compaction (open)

OpenRig's refocus hook re-sends the doctrine files after compaction or heavy
transcript growth, because a running session never re-reads files from disk. A
lead running for hours needs the current milestone and the method again after
it compacts.

Change: check whether the client has a hook that fires after compaction. If it
does, the hook prints the milestone file and a summary of the method. If none
can, the wake driver (next) includes them in its resume prompt.

### 6. A driver that wakes the lead (open, needs a decision first)

In OpenRig the lead is a live terminal session: a worker's `rig send` types
into it, so a finished worker wakes the lead. Paperclip uses heartbeats, and
OpenRig (EliasOenal) uses cron jobs. Here the lead runs only when the user
types.

Change: a thin script, not a service. It waits on dsh until a worker goes idle
or blocked, or a timer fires. Then it resumes the lead's session with a
non-interactive command of whatever client runs the lead, with a prompt such as
"continue the milestone". With kiro-cli, for example:

`kiro-cli chat --no-interactive --resume-id <lead> --agent <lead-agent> "<continue the milestone>"`

It stops when the milestone file says done or blocked, or when a budget (hours
or turns) is spent.

Prerequisites and unknowns:

- The client must be able to resume a session non-interactively. (The flags
  above exist in kiro-cli, checked in its `--help`.)
- It is not yet tested how a non-interactive run handles a tool call that isn't
  pre-approved (it probably refuses it). So the lead's command allow-list must
  first cover `git` and the build tool in the project repos. Widening it is the
  user's decision, per project.

## Already covered

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
- OpenRig (EliasOenal)'s OS-level user isolation: bwrap plus the permission
  presets already cover a single-user machine.
- OpenWork's capability marketplace: not an orchestration problem.
