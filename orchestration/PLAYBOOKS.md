# Playbooks — how the lead does each part of the job

`METHOD.md` says what the lead does and why. This file says how, step by step,
for the situations that recur. The tool mechanics are in the dsh-workers skill;
the project's paths, commands and limits are in `PROJECT.md`. When a playbook
does not fit, do what works and write a `FRICTION.md` entry.

## 1. A lead session, start to end

1. **Orient** (METHOD, *Start of every lead session*): brief, `dsh_pending`,
   `dsh_tasks`, `dsh_list` drift.
2. **Unblock first.** Anything in `dsh_pending` gets an answer or a verdict
   before you plan anything new. Workers idle on you are the most expensive
   state the team can be in.
3. **Harvest.** For each `returned` task: verify (§6), then verdict (§8).
4. **Plan.** If the human gave you work, break it into tasks (§2). If not,
   propose the next work (§2) and wait for their go before dispatching it.
5. **Dispatch** (§3) to as many workers as the work allows without file
   collisions (§9).
6. **Drive.** `dsh_wait` on the worker you expect first. It returns on
   `blocked` or turn end; handle it (§4, §5) and wait on the next.
7. **Close.** Before you end your turn to the human: every running worker is
   either working on a task you can name or idle with nothing pending; tasks
   carry notes that say where they stand; the workspace repo is checkpointed
   (METHOD, *Checkpoints*). Then report to the human (§11).

The human reads only your replies. A worker's result that you did not report
did not happen as far as they know.

## 2. Choosing and cutting work

**Where work comes from:** the human's request; the project's roadmap and QA
backlog (`PROJECT.md` names them); a rejected task; a finding a worker
reported but did not fix. Propose unrequested work, never start it: "Next on
the roadmap is X. I'd cut it into A, B, C. Go?"

**A good task:**
- has one owner and one worktree;
- fits in one worker turn, roughly under two hours or ~150 tool calls; if it
  cannot, cut it at a point where the tree is green;
- has acceptance you can check yourself, line by line:
  `make test passes`, `a scenario asserts the loader moves 30 flour`,
  `no file outside src/…/plot/ changes`. Never "works well" or "is clean";
- names the **instrument** for every invariant, not only the property.
  "No seed is lost (scenarios with counts)" was met literally with drop
  windows that could not see a loss of 4 of 6; "asserted on an exact
  level-wide count, per component, before and after each operation" could not
  have been. A green suite says the tests pass, not that they measure the
  right thing: read the assertions behind every invariant line (§6);
- says what is out of scope, so the worker does not "improve" neighbours.

**Research first when** the task depends on engine behaviour nobody has
verified, or on a choice between designs. Dispatch a read-only researcher
with the question, then cut the implementation from its answer. Research that
lands in a doc is reusable; research that lives in one worker's context is not.

**Ask for positions first when** the task makes a design decision (§7).

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

## 4. Waiting and unblocking

`dsh_wait` returns early when the worker blocks. Then `dsh_pending`:

**A question** (`dsh_answer`). Answer from, in order: `DECISIONS.md`, the
vision/design docs, the code, your own engineering judgment within the
task's scope. Answer the question asked, give the reason in one line, and
say if it changes the task. If the answer needs the human (METHOD,
*Escalate*), pick the option that is safe to proceed with ("keep both
behind the existing flag; I'm asking Elias") or tell the worker to stop and
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
| `interrupted` you did not cause | find out who (`dsh_grep`); do not assume the work was lost or finished |

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

**Context.** Compaction is normal and does not degrade work (skill). Watch for
the one real failure: `context.usedPct` climbing with no new summaries means an
orphaned compaction lock; that worker needs a restart, which is a new session:
hire, brief from its brief and its last report, retire the old one.

## 6. Verifying a returned task

A report is a claim. Before any verdict:

1. **Scope.** `git -C <worktree> diff --stat main...` lists only files the task
   could touch. A surprise file is a question for the worker, not a nit.
2. **Read the diff.** All of it for small tasks; for large ones, the parts
   behind each acceptance line and anything touching shared code.
3. **Run the checks yourself**, in the worktree, with the commands in
   `PROJECT.md`. Never accept "tests pass" from the report.
4. **Tick each acceptance line** with the evidence (a test name, a command's
   result, a file). A line you cannot tick is not met.
5. **Look for what the report did not say:** tests weakened or deleted,
   skipped/xfailed tests added, TODOs, debug output, changed tolerances,
   commented-out code.
6. `dsh_tasks action=update status=in-review` with a one-line note while you
   do it, so an interrupted session knows where it was.

If verifying would cost you many reads, dispatch a reviewer (§7) and verify
its findings instead.

## 7. Reviews and positions

**Reviewer on a finished task.** `templates/REVIEW.md`. Dispatch it in
parallel with your own verification, never instead of it, and do not hold the
implementer idle waiting for it: a review can take 30+ minutes. If your own
check finds a blocking defect first, reject on that and fold the review's
findings into the rework when they land (§8). A `read-only` reviewer
cannot build or test: its sandbox refuses every write, and builds write. So it
reads and reasons; the checks are yours (§6). If a review needs checks run, use
a verifier on `autonomous` in its own worktree of the branch, told not to edit.
Give the reviewer the task's
acceptance and the branch, not the implementer's report: it should judge the
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
`task_id`; the server logs the reopen itself. If the worker is still mid-rework
when new findings arrive (a late review), fold them in with `dsh_task
mode=steer` rather than waiting for the round to end.

**Merge**, when every line is ticked:
1. The main checkout is clean (`git status --short` empty). If not, it is the
   human's work in progress: stop and ask.
2. `git -C <repo> merge --ff-only wt/<name>`. If it will not fast-forward, the
   branch is behind: ask the worker to rebase onto `main` and re-run its
   checks (it owns its conflicts), then verify the rebased branch again. Do
   not resolve a worker's conflicts yourself unless they are trivial and you
   say so in the task note.
3. Re-run the full checks on merged `main` (`PROJECT.md`, "before every
   merge"). A merged main that fails is your bug now: revert the merge or fix
   forward immediately, and FRICTION it.
4. Remove the worktree and the branch. `dsh_tasks update status=merged` with
   the commit.
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
its role no longer exists, or it needs a restart (§5). Leave the session in
dsh; do not delete it. Its successor's brief includes the predecessor's brief,
last report, and anything in `DECISIONS.md` for that area.

## 11. Reporting to the human

Lead with the outcome, then evidence, then what needs them:

```
Done: T-004 (plot crop squares) merged as abc1234, full suite green on main.
In progress: T-005 with the implementer, blocked on nothing.
Needs you: 1) push main? 2) the bottled-oil fork (T-006 is waiting on it).
Friction: one entry (T-005's first cut was too big for one turn; split it).
```

Questions to the human are numbered, each answerable in a word or a line,
with your recommendation. Say what you verified and what you did not.
