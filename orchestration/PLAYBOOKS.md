# Playbooks — how the lead does each part of the job

`METHOD.md` says what the lead does and why. This file says how, step by step,
for the situations that recur. The tool mechanics are in the dsh-workers skill;
the project's paths, commands and limits are in `PROJECT.md`. When a playbook
does not fit, do what works and write a `FRICTION.md` entry.

## 1. A lead session, start to end

1. **Orient** (METHOD, *Start of every lead session*): brief, `dsh_pending`,
   `dsh_tasks`, `dsh_list` drift. Sweep the same way whenever you come back to
   the work (a compaction, a long wait): every `returned` task, every idle
   worker with no task, and every pending item gets an action or a reason.
2. **Unblock first.** Anything in `dsh_pending` gets an answer or a verdict
   before you plan anything new. Workers idle on you are the most expensive
   state the team can be in.
3. **Harvest.** For each `returned` task: verify (§6), then verdict (§8). If
   a batch is due, dispatch its review (§7).
4. **Plan.** If the user gave you work, break it into tasks (§2). If not,
   take the next item from the roadmap (§2), cut it and dispatch it, unless
   `PROJECT.md` says new work needs the user's go. Say what you started in
   your report.
5. **Dispatch** (§3) to as many workers as the work allows without file
   collisions (§9).
6. **Drive.** With one worker running, `dsh_wait` on it. With several,
   `dsh_wait sessions=[…all of them]`: it returns on the first to end a turn
   or block, and names it. Guessing which finishes first and waiting on that
   one leaves the other idle for up to the whole timeout. Handle what it
   returns (§4, §5), and wait again on whoever is still running.
7. **Close.** Before you end your turn to the user: every running worker is
   either working on a task you can name or idle with nothing pending; tasks
   carry notes that say where they stand; the workspace repo is checkpointed
   (METHOD, *Checkpoints*). Then report to the user (§11).

The user reads only your replies. A worker's result that you did not report
did not happen as far as they know.

## 2. Choosing and cutting work

**Where work comes from:** the user's request; the project's roadmap and QA
backlog (`PROJECT.md` names them); a rejected task; a finding a worker
reported but did not fix. Work the roadmap already orders is yours to start.
Work that is not on it, or that changes its order, you propose first: "X is
not on the roadmap, but it unblocks Y. I'd do it next unless you object." Then
go ahead if that is within your remit.

**A good task:**
- has one owner and one worktree;
- is a **whole piece**: a feature, a phase of a milestone, or a fix with its
  tests and docs, built in one sustained run. The worker chooses the build
  order and commits each green step; you do not sequence its steps for it.
  Split a piece only for a reason you write in the task note: a decision not
  yet made, two workers' files that overlap, a real fork in the road. An
  unexplained small task costs a round-trip and puts the plan in your head
  instead of the worker's. A piece may take several worker turns: when a turn
  ends before it is done, the report is progress, not a verdict, and the reply
  is "continue";
- has acceptance you can check yourself, line by line:
  `make test passes`, `a scenario asserts the loader moves 30 flour`,
  `no file outside src/…/plot/ changes`. Never "works well" or "is clean";
- names the **instrument** for every invariant, not only the property.
  "No seed is lost (scenarios with counts)" was met literally with drop
  windows that could not see a loss of 4 of 6; "asserted on an exact
  level-wide count, per component, before and after each operation" could not
  have been. A green suite says the tests pass, not that they measure the
  right thing: read the assertions behind every invariant line (§6);
- is **sized to the change.** A retune or a small fix needs the change, the
  existing suite green, and a doc line. New test classes or harnesses only
  where the task introduces an invariant the suite cannot already see. Asking
  for more is how a constants change comes back as 13 files and 750 lines;
- states the **expected size** of the whole piece (files, rough lines). It
  describes the piece; it is not a cap to cut against. Its job is to let the
  worker notice when a retune is turning into a rewrite;
- says what is out of scope, so the worker does not "improve" neighbours.

**Write numeric decisions as a formula plus one worked number**, and the
invariant it keeps ("0.375 compost per carp, before and after"). Prose like
"scales by the same factor" has several readings, and a worker that finds two
of them contradictory stops, rightly, and costs a round.

**Land the design before cutting the task.** If the design a task implements
exists only in a conversation (a worker's transcript, a chat with the user),
first write it where the team can read it: a ROADMAP paragraph or a
`DECISIONS.md` entry, and have the user confirm it. Writing it down is what
exposes contradictions ("2 per tile" and "5 seeds = 5 tiles" cannot both hold);
cutting acceptance straight from a transcript inherits them.

**Record-only items are not tasks.** A latent bug or an idea with no work
planned goes in the roadmap or the QA backlog, not `tasks.json`.

**Ids the user named:** `dsh_tasks add id=T-007` claims it. Otherwise ids go
by arrival order, and adds in one batch can swap.

**Choose the planning rigor per piece.** One ceremony for everything
over-works the trivial and under-works the hard:

| Rung | The piece gets | When |
|---|---|---|
| P0 | a few checkable outcomes and pointers to the code | small, reversible, well-trodden: a retune, a fix with a known cause |
| P1 | a full dispatch (`templates/DISPATCH.md`); the default | most pieces |
| P2 | P1, after a research round | the spec would otherwise freeze on an unknown: engine behaviour nobody has verified, an open design space |
| P3 | P2, plus positions from two workers (§7) and your decision | a wrong plan would look right from inside: load-bearing shared code, save formats, conservation rules |

The test for going up a rung is "would a wrong plan be expensive, hard to undo
and invisible from inside?", not "is this piece important?". An important
piece with no unknown and no hidden failure mode stays at P1. Say the rung in
the task note.

For P2, dispatch a read-only researcher with the question, then cut the
implementation from its answer. Research comes back as findings with sources,
not as a design; the design is yours. Research that lands in a doc is
reusable; research that lives in one worker's context is not.

## 3. Dispatching

Use `templates/DISPATCH.md`. Fill every section. The two that are skipped
most and cost most when skipped:
- **Why.** A worker that knows the purpose makes better small decisions and
  asks fewer questions.
- **Stop and ask when.** Name the decisions that are not the worker's:
  anything that touches a pillar, a public interface, another worker's files,
  or a file outside the worktree.

Mechanics:
- `dsh_tasks action=add` first, with acceptance, `branch`, `worktree`.
- Create the worktree before dispatching (`PROJECT.md`), not in the prompt.
- `dsh_task session=… task_id=… prompt=…`, `mode=queue` unless you are
  deliberately interrupting (§5).
- One task per dispatch. A second task queued behind the first loses the
  first's report in the noise; dispatch it after the verdict.
- **Model and reasoning effort are cost knobs.** Choose them per task, while
  the worker is idle: `dsh_models session=… reasoning_effort=…`, and
  `provider=… model=…` for a different model. A well-specified task (a
  retune, a mechanical change) may not need the highest effort or the
  largest model; debugging, design and load-bearing shared code may. `PROJECT.md`
  lists the models on offer, their prices, and which choices are yours. A
  switch changes only that worker; the server restores the global default.
  Record the choice in the task note, switch back when the task ends, and
  note in `FRICTION.md` whether the dearer choice saved rounds: that record
  is the only way to know whether it pays.

## 4. Waiting and unblocking

`dsh_wait` returns early when the worker blocks. Then `dsh_pending`:

**A question** (`dsh_answer`). Answer from, in order: `DECISIONS.md`, the
vision/design docs, the code, your own engineering judgment within the
task's scope. Answer the question asked, give the reason in one line, and
say if it changes the task. If the answer needs the user (METHOD,
*Escalate*), pick the option that is safe to proceed with ("keep both
behind the existing flag; I'm asking the user") or tell the worker to stop and
report, then note the open question on the task. If the answer will matter
again, `DECISIONS.md` it now, not at the end of the session.

**An approval** (`dsh_approve`). Judge the arguments (METHOD). Common calls:

| The call | Verdict |
|---|---|
| builds/tests/reads inside its worktree | allow (a well-set preset rarely asks) |
| writes outside its worktree | reject unless the task says so; tell it where to write |
| `git push`, tag, release, publish | reject; "the lead merges and pushes" |
| network fetch | allow only if the task is research that needs it, and the URL is what it says |
| deleting files | allow inside its own worktree for files it created; otherwise reject and ask why |
| anything you do not understand | reject, and ask the worker what it is for |

After a rejection, always tell the worker what to do instead, in the same
answer or a `dsh_task mode=queue`. A bare rejection makes it retry the same
thing in a different form.

## 5. When a turn ends badly, or goes the wrong way

Read the turn-end reason, not the reply alone (skill, *Turn-end reason*).

| Ending | Do |
|---|---|
| `completed` with a report | verify (§6) |
| `completed`, empty or no report | ask for the report: "Report T-00n in the usual shape." The work may be done; do not redo it |
| `error` | `dsh_status` for the error. Transient (provider, network): re-send "Continue T-00n from where you stopped." Repeated: FRICTION entry, then decide whether to cut the task smaller |
| `max-tokens` | "Continue; finish T-00n and report." If it recurs, the task is too big: cut it |
| `error` with `data_inspection_failed` | the provider's input filter rejected the history; every retry fails too. Do not re-send. `dsh_fork after_turn=<last good turn>`, then name, permission, adopt and re-brief the fork, and retire the old session (skill, *A provider can reject a session*) |
| `interrupted` you did not cause | find out who (`dsh_grep`); do not assume the work was lost or finished |
| `error` with a billing or quota failure (e.g. `QUOTA`, HTTP 402, "Insufficient Balance") | every request fails until the account changes, and paying is the user's call. Do not retry. Record each affected worktree's state on its task, set the tasks `blocked` with the reason, checkpoint, keep going with workers on other providers, and put it under "Needs you" |

**Wrong direction mid-turn.** You notice (from `dsh_status` or a question)
that it is solving the wrong problem or editing out of scope:
- small correction, still useful work: `dsh_task mode=steer` with the one-line
  correction; do not cancel.
- fundamentally wrong: `dsh_cancel`, confirm it reports `cancelled` (the
  receipt means nothing, skill), then tell it what was wrong and what to do.
  Check the worktree: it may hold half-done edits to keep or `git restore`.

**Stuck.** The same question twice, the same failing command three times, or
a loop of edits and reruns with no progress: stop it and ask for a position
("What have you tried, what do you think is wrong, what would you try next?").
Answer that, or hand the diagnosis to a fresh read-only worker. Do not
re-send the same prompt.

**Context.** High context pressure is not a reason to replace a worker.
dsh compacts a session by itself when it reaches 80% of the model's window (the
default, `thresholdRatio`; check `dsh_status` and the deployment config if in
doubt) and keeps the most recent 16% plus a summary. A worker at 79% is about
to compact, not at a ceiling. A compacted worker keeps what it learned in dense
form and needs no re-orientation; a new hire rebuilds that from its brief and
reports, and the first days of its work are the price. Compaction is lossy for
rejected alternatives and the reasons behind decisions, so the gate before a
long session rolls on is that those are in the repo, `DECISIONS.md` or the
roster notes, which is a request to the worker, not a hire.

The one real failure is an orphaned compaction lock: `context.usedPct` is still
above about 85% at the end of a turn, or keeps climbing turn after turn with no
new summaries. Only then does the worker need a restart, which is a new
session: hire, brief from its brief and its last report, retire the old one.

Hire for a new role, a disjoint area or parallel capacity. If you do retire a
worker, write the reason and its `usedPct` in the roster note, so a wrong call
can be found later.

## 6. Verifying a returned task

A report is a claim. Before any verdict:

0. **Start from the digest.** `dsh_digest session=…` (or `dsh_wait … digest=true`)
   shows, with no model in the loop, which files the worker edited, which
   commands ran as checks and whether they passed, **whether any check ran after
   the last edit**, errors and tool-policy refusals, and what changed in git
   since the dispatch. It cannot invent, so it is the cross-check on the
   report: a report that says "tests pass" over a digest with no check after the
   last edit is a question for the worker. Shell parsing is a heuristic, and a
   script whose body decides (`python -c`, a heredoc) shows as `opaque`; the git
   section is the reliable record of what changed. Use `dsh_grep` for anything the
   digest does not cover.
1. **Scope.** `git -C <worktree> diff --stat main...` lists only files the task
   could touch. A surprise file is a question for the worker, not a nit. Its
   last line is also the size: compare it with the task's stated size. Past
   twice the stated size without a stop and a report means the worker skipped
   its own check (worker brief); say so in the verdict, and if the same worker
   does it again, that is a `FRICTION.md` entry, not another reminder.
2. **Read the diff** behind each acceptance line, and anything touching
   shared code. The full read of everything merged is the batch review's
   (§7).
3. **Run the checks yourself**, in the worktree, with the commands in
   `PROJECT.md`. Never accept "tests pass" from the report.
4. **Tick each acceptance line** with the evidence (a test name, a command's
   result, a file). A line you cannot tick is not met.
5. **Look for what the report did not say:** tests weakened or deleted,
   skipped/xfailed tests added, TODOs, debug output, changed tolerances,
   commented-out code.
6. **Check how it worked, not only what it produced.** `dsh_grep` the worker's
   history for the habits that can kill a session or waste a turn: long text
   typed into bash (`<<'EOF'`, `<<EOF`) and byte dumps (`od -c`, `cat -A`),
   which can poison its history for the provider's input filter (skill), and
   the same suite rerun back to back only to re-read its output. Correct it in
   the verdict, plainly; it will not show up in the diff. The same rules hold
   for your own commands: a worker reading the log learns from yours.
7. `dsh_tasks action=update status=in-review` with a one-line note while you
   do it, so an interrupted session knows where it was.

This is the check before a merge. It is not the review: a task that passes
it merges, and the reviewer sees it later with its batch (§7).

## 7. Reviews and positions

**Review in batches, not per task.** Merge each task on your own check (§6),
then review what was merged together: at the end of a milestone phase or
after about five merged tasks, whichever comes first, and always before a
release. Use `templates/REVIEW.md` with the range (`<start>..main`) and every
task's acceptance. A verdict per task is a stop per task; a batch keeps the
team building while the reviewer reads.

- **Merged is not finished.** Findings become fix tasks. The merges stand,
  unless a finding is serious enough to revert (§8).
- **Review one task before its merge** only when it is P3 (§2). Dispatch that
  review in parallel with your own check, never instead of it, and do not hold
  the implementer idle waiting for it: a review can take 30+ minutes. If your
  check finds a blocking defect first, reject on that and fold the review's
  findings into the rework when they land (§8).
- **Drift is a finding.** Ask whether the merged whole still matches what the
  milestone asked for, or whether reasonable local steps added up to
  something larger. It ranks with a logic defect.
- **Label each miss by cause.** A *context gap*: the dispatch or the docs
  lacked what the worker needed, so the fix goes into the spec, the brief or
  `PROJECT.md`. A *judgment gap*: the context was there and the call was
  wrong, so the fix is a check (a test, a §6 step). Note the count in the
  batch's `FRICTION.md` entry; it tells you which to improve.

A `read-only` reviewer
cannot build or test: its sandbox refuses every write, and builds write. So it
reads and reasons; the checks are yours (§6). If a review needs checks run, use
a verifier on `autonomous` in its own worktree of the branch, told not to edit.
Give the reviewer the tasks'
acceptance and the range, not the implementers' reports: it should judge the
work, not the story about the work. Then:
- every finding it ranks as blocking, check yourself before sending it back;
- disagreement between reviewer and implementer is signal. Ask each to answer
  the other's strongest point in one paragraph, then decide;
- a review that finds nothing on non-trivial work gets one follow-up: "What
  would most likely break first in production?" If still nothing, note it in
  `roster` notes; a reviewer that never finds anything is not reviewing.

**Positions before a design decision.** Same question to the implementer and
the reviewer, separately (`dsh_ask`), in this shape:

```
Decision: <one line>. Options: A) … B) … (or propose C).
Which, and what breaks under the one you did not pick?
Under 1200 characters. Cite files or docs for any claim about the code.
```

You decide. Record it in `DECISIONS.md` with the rejected option and why.
Tell both workers the decision (§9) so neither keeps arguing it in code.

## 8. Verdicts and merging

**Reject** with: what failed (the acceptance line, the evidence), what would
pass, and whether to amend or add commits. Same worker, same worktree.
`dsh_tasks update status=rejected note=…`, then re-dispatch with the same
`task_id`; the server logs the reopen itself. **Two calls, in that order, never
in one parallel batch:** batched, the dispatch can land first, and the record
then says a task being reworked is rejected. If the worker is still mid-rework
when new findings arrive (a late review), fold them in with `dsh_task
mode=steer` rather than waiting for the round to end.

**Merge**, when every line is ticked:
1. The main checkout is clean (`git status --short` empty). If not, it is the
   user's work in progress: stop and ask.
2. `git -C <repo> merge --ff-only wt/<name>`. If it will not fast-forward, the
   branch is behind: ask the worker to rebase onto `main` and re-run its
   checks (it owns its conflicts), then verify the rebased branch again. Do
   not resolve a worker's conflicts yourself unless they are trivial and you
   say so in the task note.
3. Re-run the full checks on merged `main` (`PROJECT.md`, "before every
   merge"). A merged main that fails is your bug now: revert the merge or fix
   forward immediately, and FRICTION it. One exception: if the merge is local
   and unpushed, it may stay while a fix is chased, provided nothing is pushed
   from it, no branch is cut from it, and the task note says so. Reverting a
   local fast-forward costs more than it protects.
4. Before removing the worktree, read the report's last section for logs or
   other files it names and copy them out (untracked and ignored files, such as
   build logs, go with the worktree). Then remove the worktree and the branch.
   `dsh_tasks update status=merged` with the commit.
5. Inform affected workers (§9). Push only as `PROJECT.md` allows.

**Abandon** a task that no longer makes sense, with the reason. Keep the
branch if it holds anything worth reading later, and say so in the note.

## 9. Parallel work and keeping the team informed

- **Ownership.** Before dispatching in parallel, write each worker's file
  scope in its task note ("owns `src/…/plot/`, may not touch `MachineCore`").
  Two tasks that must edit the same file are sequential, not parallel.
- **Shared files** (build files, registries, the main mod class, changelogs):
  one owner per round, or leave them to the last task, or apply those edits
  yourself at merge time.
- **After every merge**, ask: does any other worker's branch touch these
  files, or depend on what changed? If yes, `dsh_task mode=queue`:
  "`<commit>` on main changes <X>. Rebase before your next commit / no action
  needed because …".
- **A decision that changes a worker's standing orientation** goes into
  `DECISIONS.md` first, then to the worker with a pointer to the entry.
- **Team size.** Start with one implementer and one reviewer. Add an
  implementer only when you have a second task with a disjoint file scope and
  your turns are not already the bottleneck.

## 10. Retiring and restarting workers

Retire (set `roster` notes, no more dispatches) when its area is finished,
its role no longer exists, or it needs a restart because of a compaction
failure (§5). High context alone is not a reason: the worker compacts itself. Leave the session in
dsh; do not delete it. Its successor's brief includes the predecessor's brief,
last report, and anything in `DECISIONS.md` for that area.

## 11. Reporting to the user

Lead with the outcome, then evidence, then what you decided, then only what
truly needs them:

```
Done: T-004 (plot crop squares) merged and pushed as abc1234, full suite green.
In progress: T-005 with the implementer, blocked on nothing.
Decided (overrule if you disagree): 2 seeds per tile; clear refunds planted seeds.
Needs you: the bottled-oil fork (T-006 waits on it; my pick is bottles, because …).
Friction: one entry (T-005 was cut into three tasks with no reason given; next time, one).
```

"Needs you" holds only what `PROJECT.md` reserves for the user. Each question
is numbered, answerable in a word or a line, and carries your recommendation.
Everything else goes under "Decided", already acted on. Say what you verified
and what you did not.
