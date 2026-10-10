Review <batch name>: the range <start>..<end> on main in <repo>. It merged
T-<a> … T-<b>.
(For a single P3 task before its merge, give branch <branch> in <worktree>
instead, and use `git -C <worktree> diff main...<branch>` below.)

What this batch was for: <the milestone or phase, in two lines>.

Each task, as given to the implementer:
<What, Why, Acceptance and Out of scope for each task, copied from the
dispatches. Do not copy the implementers' reports. You judge the work, not the
account of it.>

Read `git -C <repo> diff <start>..<end>` and the code that it touches or
depends on. You are read-only. Do not edit. Do not try to build or test: the
sandbox refuses the writes that a build makes. Reason from the code. Say where a
claim needs a run to confirm.

Work in this order. After about 40 tool calls (80 for a batch), report what you
have, even if you did not finish. Say what you did not reach.
1. List the variants that the tests exercise, and the variants that they do
   not. Variants are, for example, each direction, input kind, ordering, size,
   empty, one and many, and each way two things can be combined. Do this
   before you judge anything. A green suite is evidence only about the first
   list. Then say which of the second list the code could get wrong.
2. Read the acceptance lines that state invariants (conservation, save and
   load, concurrency, anything with "never" or "always"). Does each test
   measure the invariant, or can it pass while the invariant fails? A count of
   what was not lost cannot show a livelock. Check for a throughput or
   termination assertion.
3. Read the code paths that the tests do not reach.
4. Read everything else.

Answer:
1. For each acceptance line: met, not met, or cannot tell from the code. Give
   the file and line that show it.
2. Findings, most serious first. For each finding, give:
   - the level: blocking, should-fix or nit
   - the file and line
   - what goes wrong, and in what situation
   - the cause: a context gap (the task or the docs lacked what the worker
     needed) or a judgment gap (the context was there and the decision was
     wrong)
3. Anything outside the stated scope that changed.
4. Drift: does the whole still match what the batch was for? If it grew into
   something larger or different, say which parts.
5. What is most likely to break first in real use.

You may agree, but only after you check. "Looks good" with no findings on a
non-trivial change is not a review. If you find nothing blocking, say what you
checked. Use under 2500 characters for one task and under 4000 for a batch.
