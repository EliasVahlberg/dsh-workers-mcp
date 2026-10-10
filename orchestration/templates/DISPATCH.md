T-<nnn>: <title>

What: <The change, in two or three sentences. Be concrete: name the files, the
behaviour and the test that will show it.>

Why: <What the change is for, and which document or decision it comes from.
One or two lines. Give enough for the worker to make small decisions without
asking.>

Where: worktree <path>, branch <branch>. Commit there in small steps. Each
commit must leave the tests green. Never push.

How: You choose the build order. Read what you need and decide the steps.
Commit each step when the tests are green. Continue until the whole task is
done. If your turn must end first, report where you are and what comes next.
Ask when you are stuck.

Acceptance (the lead checks each line itself, without relying on your report):
- <A checkable line, for example `make test` passes.>
- <A checkable line, for example a test in tests/… asserts X.>
- <For an invariant, the instrument. For example: "an exact row count per
  table, asserted before and after each operation".>
- <The variants, listed. For example: "for each of GET, POST, PUT and
  DELETE", or "empty, one and many rows". For flow or progress, add a
  throughput or termination assertion, not only a count.>
- <A scope line, for example: only files under src/billing/ change.>

Expected size of the whole task: <for example 6 files, about 500 lines>.
Before each commit, run `git diff --stat main...`. If the total is more than
twice the expected size, stop and report the reason before you continue. Put
ideas outside the task in the report, not in the diff.

Out of scope: <Neighbouring things the worker must not fix or refactor. For
each one, write "report it".>

Stop and ask when: <The decisions that are not the worker's: a pillar, a public
interface, another worker's files, anything outside the worktree, a test that
must be weakened to pass.>

Read first, if you have not already: <The one or two documents or files that
matter. Skip what the worker's brief already covered.>

Report in under 2000 characters:
- what changed (files, and commits on <branch>)
- how you checked it (the commands you ran, and the results)
- for each acceptance line: met or not met, with the evidence
- anything you were unsure of, did not do, or noticed and left alone
