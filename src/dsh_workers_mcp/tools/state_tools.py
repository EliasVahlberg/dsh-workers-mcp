"""The tools that exist only with a state directory: the roster, the tasks, the digest and the brief."""

from __future__ import annotations

import json

from dsh_workers_mcp import brief as brief_mod
from dsh_workers_mcp import state as state_mod
from dsh_workers_mcp.state.core import State
from dsh_workers_mcp.tools.base import SESSION_ARG, Context, Tool


def _state(ctx: Context) -> State:
    assert ctx.state is not None, "the server checks needs_state before it calls a handler"
    return ctx.state


def _json(value: object) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False)


def handle_dsh_roster(ctx: Context, a: dict) -> str:
    return _json(state_mod.cmd_roster(_state(ctx), a))


def handle_dsh_tasks(ctx: Context, a: dict) -> str:
    return _json(state_mod.cmd_tasks(_state(ctx), a))


def handle_dsh_digest(ctx: Context, a: dict) -> str:
    return _json(state_mod.cmd_digest(_state(ctx), a))


def handle_dsh_brief(ctx: Context, a: dict) -> str:
    return _json(brief_mod.cmd_brief(_state(ctx), a))


TOOLS: list[Tool] = [
    Tool(
        name="dsh_roster",
        description=(
            "Keep the lead's roster of workers (roster.json in the state directory). "
            "The server fills in identity, preset, model, goal, last turn end and "
            "current task from the calls that it observes. You add judgment: a role, "
            "notes, and the retired flag. 'show' lists the workers. With `session`, "
            "it shows one worker and its last observed state (last prompt and reply "
            "with times, context, blocked). 'adopt' adds an existing session to the "
            "roster. dsh_create, dsh_task and dsh_ask adopt automatically. 'note' "
            "appends a note with a timestamp. 'set' changes the role, the retired "
            "flag, or both."
        ),
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["show", "adopt", "note", "set"],
                           "default": "show"},
                "session": SESSION_ARG,
                "role": {"type": "string"},
                "text": {"type": "string", "description": "for 'note'"},
                "retired": {"type": "boolean", "description": "for 'set'. Retiring a worker "
                            "below ~85% context returns a warning: dsh compacts by itself at "
                            "~80%, so context is not a reason to retire."},
                "include_retired": {"type": "boolean", "default": False},
            },
        },
        summary="managed workers: facts that the server fills in, plus your role and notes",
        blocks="no",
        handler=handle_dsh_roster,
        needs_state=True,
    ),
    Tool(
        name="dsh_tasks",
        description=(
            "Task records (tasks.json in the state directory). 'add' creates a task "
            "with status open and returns its id. Pass `id` to claim a specific id, "
            "for example one that the user named, so that several adds in one batch "
            "cannot swap ids. Pass the id as task_id to dsh_task. The server then "
            "records the dispatch and sets the status to dispatched. It sets blocked "
            "when a wait finds the worker waiting for you. It sets returned when the "
            "turn ends. These statuses are facts. You set judgment with 'update': "
            "in-review, merged, rejected (a new dispatch reopens it), abandoned, "
            "done (closed without a merge, for example a decision taken or research "
            "delivered), or back to open. Setting merged, rejected, abandoned or "
            "done needs a `note`: the commit, the reason, or the outcome. 'update' also "
            "sets notes, owner, branch, "
            "worktree, acceptance and depends_on. 'list' hides closed tasks unless "
            "you pass include_closed or a status filter. At each dispatch the server "
            "records the worker's model, effort and permission. At the end of the "
            "turn it records the usage of that dispatch (tokens, steps, wall "
            "minutes, context change). It records only what dsh reports. 'show' "
            "returns the record and a `usage` rollup (rounds, models, totals). "
            "'report' groups tasks by the model and effort of their first dispatch, "
            "with rounds per task. It measures work, not money."
        ),
        schema={
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "enum": ["list", "show", "add", "update", "report"],
                           "default": "list"},
                "id": {"type": "string"},
                "title": {"type": "string"},
                "status": {"type": "string", "enum": list(state_mod.TASK_STATUSES)},
                "owner": SESSION_ARG,
                "acceptance": {"type": "string",
                               "description": "what 'done' means, checkable"},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "branch": {"type": "string"},
                "worktree": {"type": "string"},
                "note": {"type": "string"},
                "include_closed": {"type": "boolean", "default": False},
            },
        },
        summary="task records: dispatch and return as the server tracks them, plus your verdicts",
        blocks="no",
        handler=handle_dsh_tasks,
        needs_state=True,
    ),
    Tool(
        name="dsh_digest",
        description=(
            "Summarize what a managed worker did in its last turns. The server "
            "computes the digest from the mirrored event log, without a model. The "
            "digest lists:\n"
            "- the files edited (from edit and write calls)\n"
            "- the shell commands, by kind\n"
            "- the commands that ran as checks (tests, builds, lint), each with ok "
            "or failed and the last output line\n"
            "- whether any check ran after the last edit\n"
            "- the errors and the tool-policy refusals\n"
            "- any goal that the worker created itself (`goalsCreatedByWorker`). A "
            "goal starts autonomous rounds after the turn ends\n"
            "- if the task has a worktree, what changed in git since the dispatch "
            "(commits, files, lines)\n"
            "A command counts as a check if it matches the task's acceptance line, "
            "a built-in pattern, or .orchestrator/checks.txt. The shell parsing is "
            "a heuristic. The digest lists a script whose body decides the result "
            "(python -c, heredocs) as opaque and does not guess. The git section is "
            "the reliable record of what changed. When a worker returns, use the "
            "digest before you read history. For anything the digest does not "
            "cover, use dsh_grep."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "turns": {"type": "integer", "default": 1,
                          "description": "how many of the last turns to cover"},
                "task_id": {"type": "string",
                            "description": "default: the worker's current or last task"},
            },
            "required": ["session"],
        },
        summary="what a worker did in its last turns, computed mechanically (no model)",
        blocks="no",
        handler=handle_dsh_digest,
        needs_state=True,
    ),
    Tool(
        name="dsh_brief",
        description=(
            "Run the start-of-session sweep in one call. Call it at the start of a "
            "lead session, after a compaction, and after a long wait. The tool "
            "refreshes the roster from dsh. Then it returns `needsAction` first, "
            "with one line for each of these:\n"
            "- a task that returned and waits for your verdict\n"
            "- a task blocked on you\n"
            "- an in-review task left open\n"
            "- a request pending in dsh, with its rpc_id\n"
            "- a worker that is idle with no task\n"
            "- a worker that dsh no longer has\n"
            "- drift from changes made outside this server\n"
            "After that it returns one line per active worker (role, running or "
            "idle, task, last turn end, context percent). It also returns one line "
            "per open task (status and age, owner). For a task in progress, the "
            "line adds the size of the worktree since the dispatch. If the state "
            "directory holds an executable `brief-extra`, the tool appends its "
            "output under `project`. If dsh is down, the tool answers from the last "
            "observed state and says so. live=false skips the refresh and the "
            "pending read, which takes about 2 seconds."
        ),
        schema={
            "type": "object",
            "properties": {
                "live": {"type": "boolean", "default": True,
                         "description": "refresh from dsh and read pending requests"},
            },
        },
        summary="the start-of-session sweep: what needs you, then the roster and the open tasks",
        blocks="no",
        handler=handle_dsh_brief,
        needs_state=True,
    ),
]
