Review <batch name>: the range <start>..<end> on main in <repo>, which
merged T-<a> … T-<b>.
(For a single P3 task before its merge: branch <branch> in <worktree>, and
`git -C <worktree> diff main...<branch>` below instead.)

What this batch was for: <the milestone or phase, in two lines>.

Each task, as given to the implementer:
<What / Why / Acceptance / Out of scope for each, copied from the dispatches.
Not the implementers' reports: judge the work, not the account of it.>

Look at `git -C <repo> diff <start>..<end>` and whatever code it touches or
depends on. You are read-only: do not edit, and do not try to
build or test (the sandbox refuses the writes a build makes). Reason from the
code; say where a claim would need a run to confirm.

Work in this order, and report what you have when you reach about 40 tool
calls, even if unfinished (say what you did not get to):
- first, the acceptance lines that state invariants (conservation, save/load,
  concurrency, anything "never"/"always"): does each test actually measure
  it, or could it pass while the invariant fails?
- then the code paths those tests do not reach;
- then everything else.

Answer:
1. Each acceptance line: met, not met, or cannot tell from the code, with the
   file and line that shows it.
2. Findings, most serious first. For each: blocking / should-fix / nit, the
   file and line, what goes wrong and in what situation, and its cause:
   context gap (the task or the docs lacked what was needed) or judgment gap
   (the context was there and the call was wrong).
3. Anything outside the stated scope that changed.
4. Drift: does the whole still match what the batch was for, or did it grow
   into something larger or different? Say which parts, if so.
5. What would most likely break first once this is used for real.

Agreeing is fine when it is earned, but "looks good" with no findings on a
non-trivial change is not a review. If you find nothing blocking, say what you
checked to reach that. Under 2500 characters for one task, 4000 for a batch.
