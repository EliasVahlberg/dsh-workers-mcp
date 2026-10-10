# Playbooks: how the lead does each part of the job

`METHOD.md` says what the lead does and why. This file says how, step by step,
for the situations that recur. The dsh-workers skill has the tool mechanics.
`PROJECT.md` has the paths, commands and limits of the project. When a playbook
does not fit, do what works and write a `FRICTION.md` entry.

## 1. A lead session, start to end

1. **Orient** (METHOD, *Start of every lead session*). Use the brief,
   `dsh_pending`, `dsh_tasks` and the `dsh_list` drift. Sweep the same way each
   time you come back to the work (after a compaction or a long wait). Every
   `returned` task, every idle worker with no task, and every pending item gets
   an action or a reason.
2. **Unblock first.** Give every item in `dsh_pending` an answer or a verdict
   before you plan anything new. A worker that is idle and waits for you is the
   most expensive state the team can be in.
3. **Harvest.** For each `returned` task, verify (§6) and then give the verdict
   (§8). If a batch is due, dispatch its review (§7).
4. **Plan.** If the user gave you work, break it into tasks (§2). If not, take
   the next item from the roadmap (§2), cut it and dispatch it. The exception is
   when `PROJECT.md` says that new work needs the user's go. Say in your report
   what you started.
5. **Dispatch** (§3) to as many workers as the work allows without file
   collisions (§9).
6. **Drive.** With one worker running, call `dsh_wait` on it. With several
   workers, call `dsh_wait sessions=[…all of them]`. It returns when the first
   worker ends a turn or blocks, and it names that worker. If you guess which
   worker finishes first and wait on it, the other worker can stay idle for the
   whole timeout. Handle what the call returns (§4, §5), and wait again on the
   workers that still run.
7. **Close.** Before you end your turn to the user, check these:
   - Every running worker either works on a task that you can name, or is idle
     with nothing pending.
   - Every task has a note that says where it stands.
   - The repo of the workspace has a checkpoint (METHOD, *Checkpoints*).

   Then report to the user (§11).

The user reads only your replies. A worker's result that you did not report did
not happen, as far as the user knows.

## 2. Choosing and cutting work

**Where work comes from:** the request of the user, the roadmap and the QA
backlog of the project (`PROJECT.md` names them), a rejected task, and a finding
that a worker reported but did not fix. You can start the work that the roadmap
already orders. For work that is not on the roadmap, or that changes its order,
propose it first: "X is not on the roadmap, but it unblocks Y. I would do it
next unless you object." Then go ahead, if that is within your remit.

**A good task:**
- has one owner and one worktree.
- is a **whole piece**: a feature, a phase of a milestone, or a fix with its
  tests and docs, built in one sustained run.
  - The worker chooses the build order and commits each green step. Do not
    sequence its steps for it.
  - Split a piece only for a reason that you write in the task note: a decision
    not yet made, files of two workers that overlap, or a real fork in the road.
    A small task with no reason costs a round-trip and puts the plan in your head
    instead of the worker's.
  - A piece can take several worker turns. When a turn ends before the piece is
    done, the report is progress, not a verdict. Reply "continue".
- has acceptance that you can check yourself, line by line. Examples: `make test
  passes`, `a test asserts the importer skips a duplicate row`, `no file outside
  src/billing/ changes`. Never write "works well" or "is clean".
- names the **instrument** for every invariant, not only the property. "No row
  is lost (tests with counts)" can be met literally by a test that cannot see 4
  of 6 rows go missing. "Asserted on an exact row count per table, before and
  after each operation" cannot. A green suite says that the tests pass. It does
  not say that they measure the right thing. Read the assertions behind every
  invariant line (§6).
- **names every variant** that the behaviour must hold for. "The transfer works"
  can pass with two of four directions covered. "All four directions, each
  asserted" cannot.
  - List the axes: each direction, each input kind, each way two ranges can
    overlap or join, and empty, one and many. Put the list in the acceptance
    line.
  - A line that says "works" and does not list its variants is a dispatch defect.
    A suite that is green on the variants nobody listed says little.
  - When the line is about flow or progress ("items arrive", "the job
    finishes"), also ask for a throughput or termination assertion. A count of
    what was not lost cannot see a livelock, where the same item moves back and
    forth forever.
- is **sized to the change.** A change to constants or a small fix needs the
  change, the existing suite green, and a line in the docs. Add new test classes
  or harnesses only when the task introduces an invariant that the suite cannot
  already see. If you ask for more, a change to constants comes back as 13 files
  and 750 lines.
- states the **expected size** of the whole piece (files, rough lines). The size
  describes the piece. It is not a cap to cut against. It lets the worker notice
  when a small change turns into a rewrite. If a piece is at the top of the range
  you would accept, treat that as a sign to split it at a natural seam, and do
  not accept the size.
- says what is out of scope, so that the worker does not "improve" its
  neighbours.

**Write numeric decisions as a formula plus one worked number**, and state the
invariant that it keeps ("0.375 compost per carp, before and after"). A phrase
like "scales by the same factor" has several readings. A worker that finds two
of them contradictory stops, and it is right to stop, and it costs a round.

**Land the design before you cut the task.** The design that a task implements
can exist only in a conversation (a worker's transcript, a chat with the user).
If so, first write it where the team can read it: a ROADMAP paragraph or a
`DECISIONS.md` entry. Ask the user to confirm it. Writing it down exposes
contradictions ("2 per tile" and "5 seeds = 5 tiles" cannot both hold). If you
cut acceptance straight from a transcript, you inherit them.

**Record-only items are not tasks.** A latent bug, or an idea with no work
planned, goes in the roadmap or the QA backlog. It does not go in `tasks.json`.

**Ids that the user named:** `dsh_tasks add id=T-007` claims the id. Otherwise
the ids follow the order of arrival, and the adds in one batch can swap.

**Choose the planning rigor for each piece.** One ceremony for everything
over-works the trivial pieces and under-works the hard ones.

| Rung | The piece gets | When |
|---|---|---|
| P0 | a few checkable outcomes and pointers to the code | small, reversible, well-trodden: a change to constants, a fix with a known cause |
| P1 | a full dispatch (`templates/DISPATCH.md`). This is the default | most pieces |
| P2 | P1, after a research round | the spec would otherwise freeze on an unknown: library or platform behaviour that nobody has verified, an open design space |
| P3 | P2, plus positions from two workers (§7) and your decision | a wrong plan would look right from inside: load-bearing shared code, data formats, migrations, invariants that other code relies on |

The test for going up a rung is "would a wrong plan be expensive, hard to undo
and invisible from inside?" The test is not "is this piece important?". An
important piece with no unknown and no hidden failure mode stays at P1. State
the rung in the task note.

For P2, dispatch a read-only researcher with the question. Then cut the
implementation from its answer. Research comes back as findings with sources,
not as a design. The design is yours. Research that lands in a document is
reusable. Research that lives in the context of one worker is not.

## 3. Dispatching

Use `templates/DISPATCH.md`. Fill every section. People skip two sections most
often, and skipping them costs the most:
- **Why.** A worker that knows the purpose makes better small decisions and asks
  fewer questions.
- **Stop and ask when.** Name the decisions that are not the worker's: anything
  that touches a pillar, a public interface, the files of another worker, or a
  file outside the worktree.

Mechanics:
- Call `dsh_tasks action=add` first, with acceptance, `branch` and `worktree`.
- Create the worktree before you dispatch (`PROJECT.md`). Do not do it in the
  prompt.
- Call `dsh_task session=… task_id=… prompt=…`. Use `mode=queue`, unless you
  deliberately interrupt (§5).
- Send one task in each dispatch. If you queue a second task behind the first,
  the report of the first task gets lost in the noise. Dispatch the second task
  after the verdict.
- **Optionally, make the size stop a gate.** A stop that the lead often waives is
  not a gate. In testing, three tasks in one session passed their stated size
  without a stop, and each time the work was good enough that the lead raised the
  line and merged.
  - `scripts/size-gate.sh install` adds a pre-commit hook once for the
    repository. `scripts/size-gate.sh set --lines N [--files M]`, run in the
    worktree, sets the limit for one task. Twice the expected size is the usual
    value. The hook refuses a commit that takes the task past the limit, and it
    tells the worker to report and wait.
  - Raising the limit is a decision. Do it with `size-gate.sh set`, and write the
    reason in the task note. Do not raise it as a default reply to an overrun.
  - It is a speed bump. A worker can use `git commit --no-verify` or edit the
    limit file. The check in §6 stays.
- **Model and reasoning effort are cost knobs.** Choose them for each task,
  while the worker is idle. Call `dsh_models session=… reasoning_effort=…`. Add
  `provider=… model=…` for a different model.
  - A well-specified task (a change to constants, a mechanical change) may not
    need the highest effort or the largest model. Debugging, design and
    load-bearing shared code may need them. Do not put a stronger model on a
    whole feature while a cheaper model edits the same files in the same round
    (while both tasks are open). Files that an earlier task changed and merged need
    no such care.
  - `PROJECT.md` lists the models on offer, their prices, and which choices are
    yours.
  - A switch changes only that worker. The server restores the global default.
  - At each dispatch, the server records the model, the effort and the
    permission on the task. At the turn end, it records the work that the
    dispatch took: tokens, steps, wall minutes and the context change. It
    records only what dsh reports.
  - `dsh_tasks action=report` groups the tasks by model and effort, with the
    rounds for each task. Use it to read from data whether the dearer choice
    saved rounds. It measures work, not money. Look up prices on the pricing
    page of the provider.
  - Switch back when the task ends. In `FRICTION.md`, note what the numbers do
    not show (quality, discipline).

## 4. Waiting and unblocking

`dsh_wait` returns early when the worker blocks. Then call `dsh_pending`.

**A question** (`dsh_answer`). Answer from these sources, in this order:
`DECISIONS.md`, the vision and design documents, the code, and your own
engineering judgment within the scope of the task.
- Answer the question that was asked. Give the reason in one line. Say if the
  answer changes the task.
- If the answer needs the user (METHOD, *Escalate*), pick the option that is
  safe to proceed with ("keep both behind the existing flag; I am asking the
  user"). Or tell the worker to stop and report. Then note the open question on
  the task.
- If the answer will matter again, write it in `DECISIONS.md` now. Do not wait
  for the end of the session.
- If the question is about code that you have not read, read it first. Cite
  `file:line` in the answer. Do not answer from memory.

**An approval** (`dsh_approve`). Judge the arguments (METHOD). These are the
common calls:

| The call | Verdict |
|---|---|
| builds, tests or reads inside its worktree | allow (a well-set preset rarely asks) |
| writes outside its worktree | reject, unless the task says so. Tell the worker where to write |
| `git push`, tag, release, publish | reject: "the lead merges and pushes" |
| network fetch | allow only if the task is research that needs it, and the URL is what the worker says |
| deleting files | allow inside its own worktree for files that it created. Otherwise reject and ask why |
| anything you do not understand | reject, and ask the worker what it is for |

After a rejection, always tell the worker what to do instead. Put it in the same
answer, or send a `dsh_task mode=queue`. A bare rejection makes the worker retry
the same thing in a different form.

## 5. When a turn ends badly, or goes the wrong way

Read the turn-end reason, not only the reply (skill, *Turn-end reason*).

| Ending | Do |
|---|---|
| `completed` with a report | verify (§6) |
| `completed`, empty or no report | Ask for the report: "Report T-00n in the usual shape." The work may be done. Do not redo it |
| `error` | Call `dsh_status` for the error. If it is transient (provider, network), send "Continue T-00n from where you stopped." If it repeats, write a FRICTION entry, and then decide whether to cut the task smaller |
| `max-tokens` | Send "Continue. Finish T-00n and report." If it happens again, the task is too big. Cut it |
| `error` with `data_inspection_failed` | The input filter of the provider rejected the history. Every retry fails too. Do not send it again. Call `dsh_fork after_turn=<last good turn>`. Then name the fork, set its permission, adopt it and brief it again. Retire the old session (skill, *A provider can reject a session*) |
| `interrupted` that you did not cause | Find out who did it (`dsh_grep`). Do not assume that the work was lost or finished |
| `error` with a billing or quota failure (for example `QUOTA`, HTTP 402, "Insufficient Balance") | Every request fails until the account changes, and paying is the decision of the user. Do not retry. Record the state of each affected worktree on its task. Set the tasks `blocked` with the reason. Make a checkpoint. Keep going with the workers on other providers. Put the item under "Needs you" |

**Wrong direction in the middle of a turn.** You notice (from `dsh_status` or a
question) that the worker solves the wrong problem or edits out of scope.
- The correction is small and the work is still useful: send `dsh_task
  mode=steer` with the correction in one line. Do not cancel.
- The direction is fundamentally wrong: call `dsh_cancel`. Confirm that it
  reports `cancelled` (the receipt means nothing, see the skill). Then tell the
  worker what was wrong and what to do. Check the worktree. It can hold
  half-done edits that you keep or restore with `git restore`.

**Stuck.** Signs are the same question twice, the same failing command three
times, or a loop of edits and reruns with no progress. Stop the worker and ask
for a position: "What have you tried, what do you think is wrong, what would you
try next?" Answer it, or hand the diagnosis to a fresh read-only worker. Do not
send the same prompt again.

**Parked work.** A task can wait on something else, for example a merge of
another task. Write a note on the task that starts with `PARKED on <blocker>.
Resume: <first action>.` `dsh_brief` shows a note of this form on the task line,
until a later note replaces it. Use no new status. The age of the task in its
status is already on the line.

**Context.** High context pressure is not a reason to replace a worker.
- dsh compacts a session by itself when it reaches 80% of the window of the
  model. This is the default (`thresholdRatio`). If in doubt, check `dsh_status`
  and the deployment config. dsh keeps the most recent 16% and a summary.
- A worker at 79% is about to compact. It is not at a ceiling.
- A compacted worker keeps what it learned in dense form and needs no new
  orientation. A new hire rebuilds that from its brief and reports, and the
  first days of its work are the price.
- Compaction loses the rejected alternatives and the reasons behind decisions.
  So before a long session rolls on, make sure that those are in the repo,
  `DECISIONS.md` or the roster notes. This is a request to the worker, not a
  hire.

The one real failure is an orphaned compaction lock. The compaction does not run
and the context keeps filling.
- The server watches for it, so you do not compute it. The worker gets the flag
  `compactionStalled` (with the reason in words) in `dsh_roster`, `dsh_list` and
  `dsh_status` in either of these cases:
  - `context.usedPct` is above 85% at a turn end.
  - `context.usedPct` rose over three turn ends at or above 80%, with no
    `compaction/summary` between them.
- `dsh_brief` lists the flag under `needsAction`. The flag clears itself when a
  summary lands.
- The 85% and the three turn ends are the heuristic of the server. They are not
  dsh settings. (The trigger in dsh itself is 80% by default.)
- Only a flagged worker needs a restart. A restart is a new session: hire the
  worker, brief it from its brief and its last report, and retire the old one.
- A worker near 80% with no flag is healthy. The server counts summaries, not
  start and end brackets, because the brackets can be no-ops.

Hire for a new role, a disjoint area or parallel capacity. If you do retire a
worker, write the reason and its `usedPct` in the roster note, so that someone
can find a wrong decision later.

## 6. Verifying a returned task

A report is a claim. Before you give any verdict, do these steps:

0. **Start from the digest.** `dsh_digest session=…` (or `dsh_wait …
   digest=true`) shows these items, with no model in the loop:
   - the files that the worker edited
   - the commands that ran as checks, and whether they passed
   - **whether any check ran after the last edit**
   - errors and tool-policy refusals
   - goals that the worker created itself (`goalsCreatedByWorker`). Treat each one
     as a finding: the lead did not ask for it, and it starts rounds after the
     turn ends. Pause or clear it, and tell the worker why
   - what changed in git since the dispatch

   The digest cannot invent, so it is the cross-check on the report. A report
   that says "tests pass" over a digest with no check after the last edit is a
   question for the worker. The shell parsing is a heuristic. A script whose
   body decides the result (`python -c`, a heredoc) shows as `opaque`. The git
   section is the reliable record of what changed. Use `dsh_grep` for anything
   that the digest does not cover.
1. **Scope.** `git -C <worktree> diff --stat main...` must list only files that
   the task could touch. A surprise file is a question for the worker. It is not
   a nit. The last line of the output is also the size. Compare it with the size
   that the task states. If the total is more than twice the stated size, and
   the worker did not stop and report, then the worker skipped its own check
   (worker brief). Say so in the verdict. If the same worker does it again,
   write a `FRICTION.md` entry. Do not send another reminder.
2. **Read the diff** behind each acceptance line, and anything that touches
   shared code. The batch review reads everything that merged (§7).
3. **Run the checks yourself**, in the worktree, with the commands in
   `PROJECT.md`. Never accept "tests pass" from the report.
4. **Tick each acceptance line** with the evidence (a test name, the result of a
   command, a file). A line that you cannot tick is not met. A run of zero tests,
   an empty report and empty output all count as unverified.
5. **Look for what the report did not say:** tests that were weakened or
   deleted, skipped or xfailed tests that were added, TODOs, debug output,
   changed tolerances, and commented-out code.
6. **Check how the work was done, not only what it produced.** Use `dsh_grep` on
   the history of the worker for the habits that can kill a session or waste a
   turn.
   - Long text typed into bash (`<<'EOF'`, `<<EOF`) and byte dumps (`od -c`,
     `cat -A`) can poison the history for the input filter of the provider
     (skill).
   - The same suite can run again, back to back, only to read its output again.

   Correct these habits in the verdict, in plain words. They do not show in the
   diff. The same rules hold for your own commands, because a worker that reads
   the log learns from yours.
7. Call `dsh_tasks action=update status=in-review` with a one-line note while you
   do this. Then a session that is interrupted knows where it was.

This is the check before a merge. It is not the review. A task that passes it
merges, and the reviewer sees it later with its batch (§7).

## 7. Reviews and positions

**Review in batches, not for each task.** Merge each task on your own check
(§6). Then review what was merged together. Do this at the end of a milestone
phase, or after about five merged tasks, whichever comes first, and always
before a release. Use `templates/REVIEW.md` with the range (`<start>..main`) and
the acceptance of every task. A verdict for each task is a stop for each task. A
batch keeps the team building while the reviewer reads.

- **Merged is not finished.** Findings become fix tasks. The merges stand,
  unless a finding is serious enough to revert (§8).
- **Review one task before its merge** only when it is P3 (§2). Dispatch that
  review in parallel with your own check. Never use it instead of your check.
  Do not hold the implementer idle while it waits, because a review can take 30
  minutes or more. If your check finds a blocking defect first, reject on that.
  Add the findings of the review to the rework when they arrive (§8).
- **Drift is a finding.** Ask whether the merged whole still matches what the
  milestone asked for, or whether reasonable local steps added up to something
  larger. It ranks with a logic defect.
- **Label each miss by its cause.** A *context gap*: the dispatch or the docs
  lacked what the worker needed. The fix goes into the spec, the brief or
  `PROJECT.md`. A *judgment gap*: the context was there and the decision was
  wrong. The fix is a check (a test, a step in §6). Write the count in the
  `FRICTION.md` entry of the batch. It tells you which one to improve.

A `read-only` reviewer cannot build or test. Its sandbox refuses every write,
and builds write. So it reads and reasons, and the checks are yours (§6). If a
review needs checks to run, use a verifier on `autonomous` in its own worktree
of the branch, and tell it not to edit.

Give the reviewer the acceptance of the tasks and the range. Do not give it the
reports of the implementers. It should judge the work, not the story about the
work. Then:
- Check every finding that it ranks as blocking yourself, before you send it
  back.
- A disagreement between the reviewer and the implementer is a signal. Ask each
  to answer the strongest point of the other in one paragraph. Then decide.
- A review that finds nothing on non-trivial work gets one follow-up: "What would
  most likely break first in production?" If it still finds nothing, note it in
  the `roster` notes. A reviewer that never finds anything is not reviewing.

**Positions before a design decision.** Ask the implementer and the reviewer the
same question, separately (`dsh_ask`), in this shape:

```
Decision: <one line>. Options: A) … B) … (or propose C).
Which, and what breaks under the one you did not pick?
Under 1200 characters. Cite files or docs for any claim about the code.
```

You decide. Record the decision in `DECISIONS.md` with the rejected option and
the reason. Tell both workers the decision (§9), so that neither keeps arguing
it in code.

## 8. Verdicts and merging

**Reject** with these items: what failed (the acceptance line and the evidence),
what would pass, and whether to amend or to add commits. Send it to the same
worker, in the same worktree. Call `dsh_tasks update status=rejected note=…`.
Then dispatch again with the same `task_id`. The server logs the reopen itself.
**These are two calls, in that order. Never put them in one parallel batch.** In
a batch, the dispatch can land first, and the record then says that a task under
rework is rejected. Findings can arrive while the worker is still in the middle
of the rework (a late review). Add them with `dsh_task mode=steer`. Do not wait
for the round to end.

**Merge** when every line is ticked:
1. The main checkout must be clean (`git status --short` is empty). If it is
   not, check each stray file before you decide whose it is. A worker may have
   written it. Match it against the open branches and worktrees. If it is a stray
   copy of a worker's file, set it aside with a labelled `git stash`. Do not
   delete it. If it is work in progress of the user, stop and ask.
2. Run `git -C <repo> merge --ff-only wt/<name>`. If it cannot fast-forward, the
   branch is behind. Ask the worker to rebase onto `main` and run its checks
   again. (It owns its conflicts.) Then verify the rebased branch again. Do not
   resolve the conflicts of a worker yourself, unless they are trivial and you
   say so in the task note.
3. Run the full checks again on the merged `main` (`PROJECT.md`, "before every
   merge"). A merged main that fails is your bug now. Revert the merge or fix
   forward at once, and write a FRICTION entry. There is one exception. If the
   merge is local and not pushed, it may stay while you chase a fix. Three
   conditions apply: you push nothing from it, you cut no branch from it, and
   the task note says so. Reverting a local fast-forward costs more than it
   protects.
4. Before you remove the worktree, read the last section of the report for logs
   or other files that it names, and copy them out. (Untracked and ignored
   files, such as build logs, go away with the worktree.) Then remove the
   worktree and the branch. Call `dsh_tasks update status=merged` with the
   commit.
5. Inform the workers that this affects (§9). Push only as `PROJECT.md` allows.

**Abandon** a task that no longer makes sense, and give the reason. Keep the
branch if it holds anything worth reading later, and say so in the note.

## 9. Parallel work and keeping the team informed

- **Ownership.** Before you dispatch in parallel, write the file scope of each
  worker in its task note ("owns `src/billing/`, may not touch `core/db.py`").
  Two tasks that must edit the same file are sequential, not parallel.
- **Shared files** (build files, registries, the application entry point,
  changelogs): give one owner for each round, or leave them to the last task, or
  make those edits yourself at merge time.
- **After every merge**, ask: does the branch of any other worker touch these
  files, or depend on what changed? If it does, send `dsh_task mode=queue`:
  "`<commit>` on main changes <X>. Rebase before your next commit." Or: "No
  action needed, because …".
- **A decision that changes the standing orientation of a worker** goes into
  `DECISIONS.md` first. Then tell the worker, with a pointer to the entry.
- **Team size.** Start with one implementer and one reviewer. Add an implementer
  only when you have a second task with a disjoint file scope, and your own turns
  are not already the bottleneck.

## 10. Retiring and restarting workers

Retire a worker (set the `roster` notes, and send no more dispatches) when its
area is finished, when its role no longer exists, or when it needs a restart
because of a compaction failure (§5). High context alone is not a reason,
because the worker compacts itself. Leave the session in dsh. Do not delete it.
The brief of its successor includes the brief and the last report of the
predecessor, and anything in `DECISIONS.md` for that area.

## 11. Reporting to the user

Lead with the outcome. Then give the evidence, then what you decided, and then
only what really needs the user:

```
Done: T-004 (CSV export) merged and pushed as abc1234, full suite green.
In progress: T-005 with the implementer, blocked on nothing.
Decided (overrule if you disagree): exports capped at 10k rows; a failed export is retried once.
Needs you: the storage fork (T-006 waits on it; my pick is Postgres, because …).
Friction: one entry (T-005 was cut into three tasks with no reason given; next time, one).
```

"Needs you" holds only what `PROJECT.md` reserves for the user, and only what the
user can act on now. Do not list "confirm X after Y merges" while Y still runs.
In testing, the user acted on an item like that and tested something that was
not there yet. Put "when Y merges I will ask you to check X" under In progress,
and make the request in the report where Y merges. Number each
question. The user can answer each one in a word or a line. Include your
recommendation with each. Everything else goes under "Decided", already acted
on. Say what you verified and what you did not.
