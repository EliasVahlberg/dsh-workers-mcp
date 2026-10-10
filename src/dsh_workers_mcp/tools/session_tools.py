"""The tools that inspect, set up and steer sessions: list, status, search, models, create, and so on."""

from __future__ import annotations

from dsh_workers_mcp import cmd_sessions
from dsh_workers_mcp.tools.base import SESSION_ARG, Args, Context, Tool, capture


def handle_dsh_list(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_list, Args(
        ctx.client, all=a.get("include_blank", False), limit=a.get("limit", 20)))


def handle_dsh_status(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_status, Args(
        ctx.client, session=a["session"], chars=a.get("reply_chars", 2000)))


def handle_dsh_grep(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_grep, Args(
        ctx.client, session=a["session"], pattern=a["pattern"], pages=a.get("pages", 1),
        max_messages=a.get("max_messages", 200), limit=a.get("limit", 20),
        context=a.get("context", 120), case_sensitive=a.get("case_sensitive", False)))


def handle_dsh_search(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_search, Args(ctx.client, query=a["query"], chars=4000))


def handle_dsh_models(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_models, Args(
        ctx.client, session=a["session"], reasoning_effort=a.get("reasoning_effort"),
        provider=a.get("provider"), model=a.get("model")))


def handle_dsh_create(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_create, Args(
        ctx.client, cwd=a.get("cwd"), preset=a.get("preset"), session_id=None,
        provider=a.get("provider"), model=a.get("model"), permission=a.get("permission"),
        reasoning_effort=a.get("reasoning_effort")))


def handle_dsh_permission(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_permission, Args(
        ctx.client, session=a["session"], preset=a.get("preset"), force=a.get("force", False)))


def handle_dsh_cancel(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_cancel, Args(
        ctx.client, session=a["session"], timeout=a.get("timeout", 30.0)))


def handle_dsh_name(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_name, Args(ctx.client, session=a["session"], title=a["title"]))


def handle_dsh_fork(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_fork, Args(
        ctx.client, session=a["session"], after_turn=a.get("after_turn"), at_seq=a.get("at_seq")))


def handle_dsh_goal(ctx: Context, a: dict) -> str:
    return capture(cmd_sessions.cmd_goal, Args(
        ctx.client, session=a["session"], action=a.get("action", "show"),
        objective=a.get("objective"), max_rounds=a.get("max_rounds"), start=a.get("start", False)))


TOOLS: list[Tool] = [
    Tool(
        name="dsh_list",
        description=(
            "List the dsh worker sessions. Each entry has the id, the running or "
            "idle state, the agent preset, the cwd, the title, and the "
            "context-window pressure in percent. Start here to see the team."
        ),
        schema={
            "type": "object",
            "properties": {
                "include_blank": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "default": 20},
            },
        },
        summary="the roster: who exists, who is busy, context pressure",
        blocks="no",
        handler=handle_dsh_list,
    ),
    Tool(
        name="dsh_status",
        description=(
            "Show a summary of one worker. It has the running or idle state, turns, "
            "steps, LLM and tool minutes, context-window usage with a breakdown, "
            "token usage with cache-hit percent, the current goal, and the latest "
            "committed reply."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "reply_chars": {"type": "integer", "default": 2000},
            },
            "required": ["session"],
        },
        summary="deep insight into one worker, including `lastTurnEnd`",
        blocks="no",
        handler=handle_dsh_status,
    ),
    Tool(
        name="dsh_grep",
        description=(
            "Search the conversation history of one worker with a regex. The search "
            "covers the worker's messages and tool calls. It skips streaming chunk "
            "events. It reads backwards, one page at a time. To search deeper into a "
            "long session, raise `pages`."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "pattern": {"type": "string", "description": "Python regex."},
                "pages": {"type": "integer", "default": 1},
                "max_messages": {"type": "integer", "default": 200},
                "limit": {"type": "integer", "default": 20},
                "context": {"type": "integer", "default": 120},
                "case_sensitive": {"type": "boolean", "default": False},
            },
            "required": ["session", "pattern"],
        },
        summary="regex over the own history of one worker, paged backwards",
        blocks="no",
        handler=handle_dsh_grep,
    ),
    Tool(
        name="dsh_search",
        description=(
            "Search all sessions at once on the dsh server. The result has session "
            "ids with snippets. Use it to find which worker discussed a topic. To "
            "search inside one worker, use dsh_grep. The query has a maximum of 500 "
            "characters."
        ),
        schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        summary="full-text search across every session, to find who discussed a topic",
        blocks="no",
        handler=handle_dsh_search,
    ),
    Tool(
        name="dsh_models",
        description=(
            "Show the model catalog and the current model selection for a worker, "
            "with the available reasoning-effort levels. If you pass "
            "reasoning_effort (and optionally provider and model), the call changes "
            "them for that session only. The session must be idle. The call then "
            "restores dsh's global default. Effort changes the cost. A "
            "well-specified task rarely needs 'high'."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "reasoning_effort": {"type": "string",
                                     "description": "e.g. off | low | high | max; "
                                     "see the catalog for the model's levels"},
                "provider": {"type": "string"},
                "model": {"type": "string"},
            },
            "required": ["session"],
        },
        summary=("model and reasoning-effort levels. `reasoning_effort=` sets the effort of a "
                 "worker, only while it is idle"),
        blocks="no",
        handler=handle_dsh_models,
    ),
    Tool(
        name="dsh_create",
        description=(
            "Create a new worker session. You must give cwd, an agent preset, or "
            "both. An empty request would make a blank session without an error, so "
            "the server refuses it. You may pin a provider and a model. dsh saves "
            "each pin as the global default, so this tool restores the previous "
            "default and reports it as defaultModel."
        ),
        schema={
            "type": "object",
            "properties": {
                "cwd": {"type": "string"},
                "preset": {"type": "string"},
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "reasoning_effort": {"type": "string"},
                "permission": {
                    "type": "string",
                    "description": (
                        "Permission preset applied before anything is dispatched: "
                        "read-only | supervised | autonomous (danger-full-access "
                        "exists but is for the user to set). See dsh_permission."
                    ),
                },
            },
        },
        summary="create a worker, optionally at a permission preset",
        blocks="no",
        handler=handle_dsh_create,
    ),
    Tool(
        name="dsh_fork",
        description=(
            "Copy a worker into a new session that keeps the history up to a turn "
            "boundary and nothing after it. Use it to restart from a known good "
            "point. Two cases need it: a session whose later history the provider "
            "now rejects, and a session with a broken compaction. When the provider "
            "rejects the history (data_inspection_failed), every turn fails and a "
            "retry cannot help. Fork after the last turn that ended normally. "
            "after_turn=N keeps turns 1 to N. dsh forks whole turns, so at_seq keeps "
            "the complete turn that contains it. The fork is a new session with no "
            "name and no goal. Before you dispatch to it, name it, set its "
            "permission, and adopt it with dsh_roster."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "after_turn": {"type": "integer", "minimum": 1},
                "at_seq": {"type": "integer", "minimum": 0},
            },
            "required": ["session"],
        },
        summary="fork a session at a turn boundary (recovers a poisoned history)",
        blocks="no",
        handler=handle_dsh_fork,
    ),
    Tool(
        name="dsh_goal",
        description=(
            "Read or change a worker's goal. A dsh goal is an autonomous loop, not a "
            "note. The action 'set' creates the goal in the active state. The worker "
            "starts at once and works round after round (max_rounds, default 256) "
            "without a task from you. For this reason 'set' requires start=true. Do "
            "not use goals for standing orientation. Use the brief for that. 'show' "
            "reads the goal. 'edit' changes objective or max_rounds. 'pause', "
            "'resume', 'complete' and 'clear' change the phase. 'pause' does not "
            "stop a round that is running: call dsh_cancel for that. Each change "
            "uses the current revision, so dsh rejects a change that races with "
            "another edit."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "action": {"type": "string",
                           "enum": ["show", "set", "edit", "pause", "resume",
                                    "complete", "clear"],
                           "default": "show"},
                "objective": {"type": "string",
                              "description": "Required for set, optional for edit."},
                "max_rounds": {"type": "integer"},
                "start": {"type": "boolean", "default": False,
                          "description": "Required true for set: confirms you "
                          "want the worker to start working on the goal now."},
            },
            "required": ["session"],
        },
        summary="read or drive a goal, which is an autonomous loop (`set` needs `start=true`)",
        blocks="no",
        handler=handle_dsh_goal,
    ),
    Tool(
        name="dsh_cancel",
        description=(
            "Interrupt a worker's running turn. The result says whether the abort "
            "took effect, not only whether dsh accepted the request. dsh returns the "
            "same receipt for an idle session, so `cancelled` is true only when the "
            "turn ended as `aborted`. If the cancel loses a race, the work of the "
            "turn stays."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "timeout": {"type": "number", "default": 30,
                            "description": "seconds to wait for the abort to land"},
            },
            "required": ["session"],
        },
        summary="interrupt a turn, and confirm that the abort landed",
        blocks="briefly, to confirm the abort",
        handler=handle_dsh_cancel,
    ),
    Tool(
        name="dsh_name",
        description=(
            "Give a worker session a title that a person can read, so that the roster "
            "shows roles and not UUIDs. Without a name, dsh generates titles from the "
            "conversation, and long-lived workers on one project then look alike. A "
            "name that you set is pinned: dsh stops regenerating it for the life of "
            "the session. Name a worker by its standing role, not by its current "
            "task, because the title outlives the task. The title shows in "
            "dsh_list."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "title": {
                    "type": "string",
                    "description": (
                        "Collapsed to one line and truncated to 80 UTF-8 bytes; "
                        "the accepted result is echoed back. Must contain "
                        "visible characters."
                    ),
                },
            },
            "required": ["session", "title"],
        },
        summary="pin a role name on a worker, so that the roster is readable",
        blocks="no",
        handler=handle_dsh_name,
    ),
    Tool(
        name="dsh_permission",
        description=(
            "Show or set a worker's permission preset (its supervision level). "
            "read-only: the filesystem is read-only and writes are denied. Use it "
            "for reviewers. supervised: writes stay inside the workspace. Web "
            "access, git push and sandbox escalations ask you, and the requests "
            "appear in dsh_pending. autonomous: the same write limit, but every "
            "request that would ask is rejected, so the worker never blocks and "
            "needs no monitoring. danger-full-access: no limits. Leave this preset "
            "to the user. Presets control scope. They are not security: the sandbox "
            "limits writes only, not reads or network, and rules that match command "
            "text can be evaded. The tool refuses to change a worker in the middle "
            "of a turn unless force is set."
        ),
        schema={
            "type": "object",
            "properties": {
                "session": SESSION_ARG,
                "preset": {"type": "string",
                           "description": "Omit to read the current preset."},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["session"],
        },
        summary="show or set the supervision level of a worker",
        blocks="no",
        handler=handle_dsh_permission,
    ),
]
