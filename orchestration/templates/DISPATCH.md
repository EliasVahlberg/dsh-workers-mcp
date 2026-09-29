T-<nnn>: <title>

What: <the change, in two or three sentences. Concrete: files, behaviour,
the test that will show it.>

Why: <what it is for, and which doc or decision it comes from. One or two
lines; enough that the worker can make small calls itself.>

Where: worktree <path>, branch <branch>. Commit there, in small green steps.
Never push.

Acceptance (the lead checks each line itself):
- <checkable line, e.g. `make test` passes>
- <checkable line, e.g. a scenario in tests/… asserts X>
- <for an invariant, the instrument: e.g. "an exact level-wide count per
  component, asserted before and after each operation">
- <scope line, e.g. only files under src/…/plot/ change>

Expected size: <e.g. 2 files, under ~150 lines>. If it is heading past twice
that, stop and report why before going on. Ideas beyond the task go in the
report, not in the diff.

Out of scope: <neighbouring things it must not fix or refactor; say "report
it" instead>.

Stop and ask when: <the decisions that are not the worker's: a pillar, a
public interface, another worker's files, anything outside the worktree, a
test that has to be weakened to pass>.

Read first, if you have not: <the one or two docs or files that matter;
skip what its brief already covered>.

Report in under 2000 characters:
- what changed (files, commits on <branch>)
- how you verified it (commands run, results)
- each acceptance line: met / not met, with the evidence
- anything you were unsure of, did not do, or noticed and left alone
