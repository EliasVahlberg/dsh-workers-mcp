"""
The two hooks around every tool call: `before_call` can refuse a call, and
`after_call` records what a successful call established.
"""

from __future__ import annotations

import contextlib
import json

from dsh_workers_mcp import dshctl
from dsh_workers_mcp.state.core import State
from dsh_workers_mcp.state.reports import digest_for
from dsh_workers_mcp.state.store import now_iso
from dsh_workers_mcp.state.usage import dispatch_facts
from dsh_workers_mcp.transport import DshClient


def _session_of(arguments: dict, result: object, client: DshClient) -> str | None:
    if isinstance(result, dict) and isinstance(result.get("sessionId"), str):
        return result["sessionId"]
    ref = arguments.get("session")
    if not ref:
        return None
    return dshctl.find_session(ref, client)["sessionId"]


# Tools whose call means "this session is doing work for us": adopt it.
ADOPTING = {"dsh_create", "dsh_task", "dsh_ask"}
# Tools after which a managed session is re-observed.
OBSERVING = {"dsh_create", "dsh_task", "dsh_ask", "dsh_wait", "dsh_status",
             "dsh_name", "dsh_permission", "dsh_goal", "dsh_cancel",
             "dsh_answer", "dsh_approve"}


def before_call(state: State, tool: str, arguments: dict) -> None:
    """Validation that must happen before a tool acts. Raises to refuse the call."""
    if tool == "dsh_task" and arguments.get("task_id"):
        session_id = dshctl.find_session(arguments["session"], state.client)["sessionId"]
        state.check_dispatch(arguments["task_id"], session_id)


def _annotate_list(state: State, result: list) -> list:
    """Add the roster entry to each row of dsh_list, and the roster's workers that dsh lost."""
    summaries = state.client.rpc("session.list").get("items", [])
    report = state.reconcile(summaries)
    listed = {row["sessionId"] for row in result}
    for row in result:
        if row["sessionId"] in report:
            row["roster"] = report[row["sessionId"]]
    for sid, entry in report.items():
        if sid not in listed and entry.get("missing"):
            result.append({"sessionId": sid, "roster": entry})
    return result


def _created_facts(state: State, arguments: dict, result: dict, session_id: str) -> dict:
    """What dsh_create established: when, the model and the permission that was set."""
    auto: dict = {"createdAt": now_iso()}
    if result.get("selectedModel"):
        auto["model"] = result["selectedModel"]
    else:
        with contextlib.suppress(dshctl.RpcError):
            current = state.client.rpc("session.models", {"sessionId": session_id}).get("current") or {}
            if current:
                auto["model"] = f"{current.get('provider')}/{current.get('model')}"
    if arguments.get("permission"):
        auto["permissionSet"] = arguments["permission"]
    return auto


def _auto_facts(state: State, tool: str, arguments: dict, result: object, session_id: str) -> dict:
    """The facts about a worker that one successful tool call established."""
    if tool == "dsh_create" and isinstance(result, dict):
        return _created_facts(state, arguments, result, session_id)
    if tool == "dsh_name" and isinstance(result, dict):
        return {"name": result.get("title")}
    if tool == "dsh_permission" and arguments.get("preset"):
        return {"permissionSet": arguments["preset"]}
    return {}


def _enriched_result(state: State, tool: str, arguments: dict, session_id: str,
                     result: object) -> dict | None:
    """The result with more in it, for the two tools whose output is enriched. Otherwise None."""
    if not isinstance(result, dict):
        return None
    # Opt-in: a wait that ended a turn can carry the digest, so the usual
    # wait-then-status-then-grep sequence becomes one call.
    if (tool == "dsh_wait" and arguments.get("digest") and result.get("completed")
            and not result.get("blocked")):
        return {**result, "digest": digest_for(state, session_id, observe=False)}
    stalled = state.session_state(session_id).get("compaction", {}).get("stalled")
    if tool == "dsh_status" and stalled:
        return {**result, "compactionStalled": stalled}
    return None


def after_call(state: State, tool: str, arguments: dict, text: str) -> dict | list | None:
    """
    Record what a successful tool call established. Returns the (possibly
    annotated) parsed result for tools whose output is enriched, else None.
    """
    try:
        result = json.loads(text)
    except (TypeError, ValueError):
        result = None

    if tool == "dsh_list" and isinstance(result, list):
        return _annotate_list(state, result)
    if tool not in OBSERVING:
        return None
    session_id = _session_of(arguments, result, state.client)
    if not session_id:
        return None
    if tool in ADOPTING:
        state.adopt(session_id, how=tool)
    if not state.is_managed(session_id):
        return None

    auto = _auto_facts(state, tool, arguments, result, session_id)
    if auto:
        state._update_worker(session_id, auto=auto)
    if tool == "dsh_task" and arguments.get("task_id") and isinstance(result, dict):
        state.record_dispatch(arguments["task_id"], session_id, arguments.get("prompt", ""),
                              arguments.get("mode", "queue"), result.get("seqBefore"),
                              facts=dispatch_facts(state, session_id))
    blocked = result.get("pending") if isinstance(result, dict) and result.get("blocked") else None
    state.observe(session_id, blocked=blocked, unblocked=tool in ("dsh_answer", "dsh_approve"))
    return _enriched_result(state, tool, arguments, session_id, result)

