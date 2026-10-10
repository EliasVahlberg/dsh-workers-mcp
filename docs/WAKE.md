# Waking the lead

The lead runs only when a person types, so a worker that returns or blocks waits for the user. `dsh-wake` is a thin loop that closes this gap. It reads the same data as `dsh_brief`. When `needsAction` has items that it did not show the lead at the last wake, it runs a command that you give it. The command starts the client of the lead non-interactively with a resume prompt.

```bash
dsh-wake --cmd 'my-client --resume --no-interactive {prompt}' \
         --state-dir .orchestrator --milestone MILESTONE.md \
         --budget-hours 8 --max-fires 50 --interval 60
dsh-wake --cmd '…' --dry-run      # one pass: print what it would run, run nothing
```

`{prompt}` and `{state_dir}` are filled in and quoted for the shell. The driver is not tied to one client. The rules:
- It wakes the lead only if `needsAction` has lines, and only if they differ from the lines at the last wake. A lead that cannot clear an item (it waits for the user) is not woken again for that item.
- It stops when the first line of the milestone file starts with `DONE` or `BLOCKED`, after `--budget-hours` hours, or after `--max-fires` wakes.
- It logs every wake, skip, stop and command exit, with the reason, to `<state dir>/wake.jsonl`.
- A wake is a hint. The woken lead starts with `dsh_brief`, and the default prompt says so.

Not verified against a real client. How a non-interactive run treats a tool call that is not pre-approved, and whether the resume flag keeps the context of the lead across wakes, depend on the client. The command allow-list of the lead may need widening for the build and VCS commands of the project. That is a decision for the user. The tests cover the rules, the loop with a fake clock and runner, and the quoting. Try your command by hand before you leave the driver running.
