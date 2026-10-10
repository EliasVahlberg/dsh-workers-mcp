# Lead reminder

This file is a short copy of the method. Load it as a standing resource of the
lead, so that the client sends it again after a compaction. The full text is in
`orchestration/METHOD.md` and `orchestration/PLAYBOOKS.md`.

After a compaction, a restart or a long wait, do these steps before you act:

1. Call `dsh_brief`. Trust the records. Do not trust your memory of them.
2. Answer everything in `dsh_pending` first. An idle worker that waits for you
   costs the most.
3. Read `PROJECT.md` and the last entries of `DECISIONS.md`.

Rules that you must not lose:

- Decide when you are confident. Escalate only what `PROJECT.md` lists under
  "What needs the user". Record each decision, and list it under "Decided" in
  your report.
- Give each task checkable acceptance, with every variant listed. Dispatch it
  with `task_id`. One task in each dispatch.
- A report is a claim. Read the digest, read the diff, and run the checks
  yourself, before you give a verdict (PLAYBOOKS §6).
- Merge only with `--ff-only`, and run the full checks again on the result.
  Workers never push. Push only as `PROJECT.md` allows.
- Never use `dsh_goal` for orientation. A goal starts autonomous rounds.
- Judge the arguments of an approval, not the reason that the worker gives.
- Do not put calls that depend on each other in one batch.
- Commit the workspace repo at the end of a session. Write each place where the
  method did not fit in `FRICTION.md`.
