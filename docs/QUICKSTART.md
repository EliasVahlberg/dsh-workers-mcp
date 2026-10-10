# Quick start

This guide takes about 15 minutes. At the end, one worker has done one task in its own git worktree, and you have merged the result.

You need Python 3.10 or later, git, [uv](https://docs.astral.sh/uv/), a running `dsh web` server, and an MCP-capable agent client. We call that agent the lead. The lead calls the tools. You review and merge.

## 1. Check the install

```bash
git clone <repo-url> dsh-workers-mcp
uv run --project dsh-workers-mcp dshctl list
```

The command prints the sessions of your dsh server. If it prints `cannot reach`, start `dsh web` first. If dsh runs on another port, add `--base http://127.0.0.1:<port>`.

## 2. Learn the tools in tiers

Start with the first tier. Add the others when you need them.

| Tier | Tools | Use |
|---|---|---|
| Core | `dsh_create`, `dsh_task`, `dsh_wait`, `dsh_status`, `dsh_pending`, `dsh_answer`, `dsh_approve` | Start a worker, give it work, wait, unblock it. |
| Tracking | `dsh_tasks`, `dsh_roster`, `dsh_digest`, `dsh_brief` | Keep a record of tasks and workers, and check the work. |
| Supervision | `dsh_permission`, the [policy overlay](../overlays/tool-policy/README.md) | Limit what a worker may do. |
| Optional | [`dsh-wake`](../README.md#waking-the-lead), [`scripts/size-gate.sh`](../scripts/size-gate.sh), [`docs/SANDBOX.md`](SANDBOX.md) | Wake the lead, limit task size, box the server. |

## 3. Wire the server into the lead

Add this to the MCP configuration of your client. Use absolute paths.

```json
{
  "mcpServers": {
    "dsh-workers": {
      "command": "uv",
      "args": ["run", "--project", "/abs/path/dsh-workers-mcp", "dsh-workers-mcp",
               "--state-dir", "/abs/path/demo/.orchestrator"],
      "timeout": 600000
    }
  }
}
```

The `--state-dir` option sets where the server records tasks and history. Restart the client. It must list the tools that start with `dsh_`.

## 4. Make a toy project

```bash
mkdir demo && cd demo
git init -b main
echo demo > README.md
git add README.md && git commit -m start
git worktree add -b w1 ../demo-w1
```

Each worker gets its own worktree. A second worker would get `w2` in `../demo-w2`.

## 5. Ask the lead to run one task

Give the lead the skill file [`skills/dsh-workers/SKILL.md`](../skills/dsh-workers/SKILL.md) if your client supports skills. Then say:

> Use the dsh-workers tools. Create a worker with cwd `/abs/path/demo-w1`. Name its role "implementer". Add a task "Add hello.txt" with worktree `/abs/path/demo-w1`, branch `w1`, and the acceptance line "hello.txt exists and is committed on w1". Dispatch the task with the prompt "Create hello.txt with the text hello and commit it." Wait for the worker. Then show me the digest.

The lead makes these calls:

| Call | What you see |
|---|---|
| `dsh_create cwd=…` | A new `sessionId`. |
| `dsh_roster action=set role=…` | The role in the roster. |
| `dsh_tasks action=add …` | A task `T-001` with status `open`. |
| `dsh_task task_id=T-001 …` | The task has status `dispatched`. The call returns at once. |
| `dsh_wait` | It returns when the turn ends, or when the worker asks you something. |
| `dsh_digest` | The commits and files that changed in the worktree, and the checks that ran. |

If `dsh_wait` returns because the worker is blocked, call `dsh_pending`. Then answer with `dsh_answer`, or approve with `dsh_approve` after you read the real arguments.

## 6. Review and merge

You do this part. The lead does not merge.

```bash
git -C ../demo-w1 log --oneline
git merge --no-edit w1
git rev-parse --short HEAD
```

Then ask the lead to close the task with `dsh_tasks action=update id=T-001 status=merged note=<the commit>`. The call needs a note. Without one it returns an error.

## 7. Check the result

Ask the lead for `dsh_brief`. It shows no open task. It lists the idle worker under `needsAction`, because the worker has no work. That is the signal to give it the next task, or to retire it.

## What to read next

- Run more than one worker, hire for a role, or handle a failed task: [`orchestration/METHOD.md`](../orchestration/METHOD.md) and [`orchestration/PLAYBOOKS.md`](../orchestration/PLAYBOOKS.md).
- Copy the project files from `orchestration/templates/` into your `.orchestrator/` directory.
- Understand the limits of a worker: [README, What a worker can and cannot do](../README.md#what-a-worker-can-and-cannot-do).

## What this guide does not prove

[`tests/test_quickstart.py`](../tests/test_quickstart.py) runs steps 3 to 7 against the real server process, a real git repository and a stand-in dsh. The stand-in has a scripted worker, so no model ran. Step 1 and the `uv run` form of step 3 were run against a real dsh with read-only calls. No one has yet followed this guide from an empty machine with a real model.
