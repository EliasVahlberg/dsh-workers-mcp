# Orchestrator state

The server records what it observes, so that the orchestrator cannot forget to. By default it writes to `.orchestrator/` in the directory where the server started. `--state-dir` overrides this. The server creates the directory on the first tool call.

```
.orchestrator/
  logs/YYYY-MM-DD.jsonl     every tool call: arguments, result, ok, duration
  history/<sessionId>/
    events.jsonl            incremental mirror of the dsh session log (chunks dropped)
    mirror.json             its lastSeq cursor
    state.json              last observed: last prompt, reply and turn end with times,
                            context, preset, goal, blocked-on
  roster.json               managed workers: `auto` (server) + `notes` (orchestrator)
  tasks.json                tasks: fields, dispatch records, status history
```

The rule is **the server writes facts, the orchestrator writes judgment**. Judgment goes only through `dsh_roster` and `dsh_tasks`, so neither side can overwrite the other:

- **Roster.** `dsh_create`, `dsh_task` and `dsh_ask` put a session on the roster. `dsh_roster action=adopt` adds an existing session. The server fills in the name, preset, model, goal, last turn end and current task from the calls that it observes. The role, the notes and the retired flag are yours. `dsh_list` compares the roster with dsh and reports **drift** (a session that someone renamed or gave another preset in the UI, or deleted). It does not disagree silently.
- **Tasks.** `dsh_tasks action=add` returns an id. Pass it as `task_id` to `dsh_task`. The server checks it before it sends anything. It refuses the dispatch if the id is unknown, closed, or owned by another worker. Then the server moves the task `dispatched → blocked → returned` as it observes the worker. `in-review`, `merged`, `rejected` and `abandoned` are the decisions of the orchestrator.
- **The digest is mechanical.** `dsh_digest` (or `dsh_wait … digest=true`, which attaches the digest when a turn ends) summarizes the last turns from the mirrored event log, with no model call. It lists:
  - the files edited (from `edit` and `write` calls)
  - the shell commands, by kind
  - the commands that ran as **checks**, each with ok or failed and the last output line
  - whether a check ran **after the last edit**
  - the errors and the tool-policy refusals
  - any goal that the worker created itself (`goalsCreatedByWorker`)
  - when the task has a worktree, what changed in git since the dispatch (the server records the state of the worktree at dispatch)

  A command counts as a check if it matches the acceptance line of the task (backticked commands), a built-in pattern for common runners, or a line in `<state dir>/checks.txt` (one regex for each line). The shell parsing is a heuristic. `python -c` and heredoc scripts show as `opaque`. A failing run behind `| tail` is recognised from its output, because the pipeline hides the exit status. The git section is the reliable record of what changed, whatever command did it.
- **Each dispatch records what it used.** With `task_id`, `dsh_task` stores the model, the reasoning effort and the permission of the worker on the task. The turn end stores the tokens, steps, wall minutes and context change of the dispatch, as dsh reports them. A field that dsh does not give is omitted, not estimated. `dsh_tasks show` adds a rollup. `dsh_tasks action=report` groups the tasks by model and effort, with the rounds for each task. It reports work, not money. Prices change, so look them up on the pricing page of the provider.
- **Writes answer briefly. `show` has everything.** The add and update actions of `dsh_tasks`, and the adopt, note and set actions of `dsh_roster`, return a short acknowledgement. For a task it has the id, status, owner, branch and note count. For a worker it has the role, the retired flag and the last note. It does not return the whole record. The caller just wrote the record, and the record with its history would sit in the context of the lead on every call. `dsh_tasks action=show` and `dsh_roster action=show session=…` return the full record.
- **Only managed sessions are mirrored.** If you inspect a session with `dsh_status`, that does not start recording it.
- **Cached state is "last observed".** Every snapshot carries `observedAt`. While dsh is up, the live tools are the source of truth.
- **Recording never changes a result.** If a call succeeded and writing the state then fails, the success stands. The server appends a `[state: …]` note. So the orchestrator never sends again work that already happened.
- **A session id must be a plain file name.** It becomes the directory `history/<sessionId>`. The server refuses an id with a path separator. Task ids must look like `T-007`. Roles, notes and titles are values in the JSON files and never become paths.

From the author's testing, on one session that you cannot reproduce: the first mirror of a session with 12,649 events (2,500 tool calls) takes 4.6 s and 24 MB. Later syncs of an unchanged session take 0.3 s. Keep `logs/` and `history/` out of version control. They hold the full transcripts of workers.
