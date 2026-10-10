"""
What a task cost in work, from the numbers that dsh reports. Fields that dsh does
not give are left out, never estimated, and no money is computed: prices change
and belong to the provider.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import datetime as _dt

from dsh_workers_mcp import sessions
from dsh_workers_mcp.transport import RpcError

if TYPE_CHECKING:
    from dsh_workers_mcp.state.core import State

# ------------------------------------------------------------------- usage
# What a task cost in work, from the numbers dsh reports. Fields dsh does not
# give are left out, never estimated, and no money is computed: prices change
# and belong to the provider.

_TOKEN_FIELDS = {"uncachedInputTokens": "uncachedInput", "cacheReadTokens": "cacheRead",
                 "outputTokens": "output"}
_STAT_FIELDS = {"steps": "steps", "llmMs": "llmMs", "toolMs": "toolMs"}


def usage_snapshot(summary: dict) -> dict:
    """Cumulative totals for a session now, from its projections. Omits what is absent."""
    values = sessions.projections(summary)
    out: dict = {}
    for source, fields in ((values.get("tokenUsage") or {}, _TOKEN_FIELDS),
                           (values.get("sessionStats") or {}, _STAT_FIELDS)):
        out.update({name: source[key] for key, name in fields.items()
                    if isinstance(source.get(key), (int, float))})
    pressure = values.get("contextPressure") or {}
    if pressure.get("contextWindow") and pressure.get("pressureTokens") is not None:
        out["contextPct"] = round(pressure["pressureTokens"] / pressure["contextWindow"] * 100, 1)
    return out


def usage_delta(before: dict | None, after: dict | None) -> dict:
    """What a dispatch used: the growth of each total both snapshots have."""
    if not before or not after:
        return {}
    out: dict = {}
    for key in (*_TOKEN_FIELDS.values(), *_STAT_FIELDS.values()):
        if key in before and key in after and after[key] >= before[key]:
            out[key] = after[key] - before[key]
    if "llmMs" in out:
        out["llmMinutes"] = round(out.pop("llmMs") / 60000, 1)
    if "toolMs" in out:
        out["toolMinutes"] = round(out.pop("toolMs") / 60000, 1)
    if "contextPct" in before and "contextPct" in after:
        out["contextPct"] = [before["contextPct"], after["contextPct"]]
    return out


def _minutes_between(start: str | None, end: str | None) -> float | None:
    if start is None or end is None:
        return None
    try:
        seconds = (_dt.datetime.fromisoformat(end) - _dt.datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None
    return round(max(seconds, 0) / 60, 1)


def dispatch_facts(state: State, session_id: str) -> dict:
    """The worker's model, effort, permission and usage totals at the moment of dispatch."""
    facts: dict = {}
    with contextlib.suppress(RpcError):
        current = state.client.rpc("session.models", {"sessionId": session_id}).get("current") or {}
        if current.get("model"):
            facts["model"] = (f"{current['provider']}/{current['model']}"
                              if current.get("provider") else current["model"])
        if current.get("reasoningEffort"):
            facts["effort"] = current["reasoningEffort"]
    with contextlib.suppress(RpcError):
        summary = sessions.find_session(session_id, state.client)
        permission = (sessions.projections(summary).get("permissions") or {}).get("currentValue")
        if permission:
            facts["permission"] = permission
        snapshot = usage_snapshot(summary)
        if snapshot:
            facts["usageBefore"] = snapshot
    return facts


def task_usage(task: dict) -> dict | None:
    """Rounds, models and summed usage over a task's dispatches (None before any dispatch)."""
    dispatches = task.get("dispatches") or []
    if not dispatches:
        return None
    totals: dict = {}
    for dispatch in dispatches:
        for key, value in (dispatch.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                totals[key] = round(totals.get(key, 0) + value, 1)
    models = []
    for dispatch in dispatches:
        label = "/".join(x for x in (dispatch.get("model"), dispatch.get("effort")) if x)
        if label and label not in models:
            models.append(label)
    return {"rounds": len(dispatches), **({"models": models} if models else {}),
            **({"total": totals} if totals else {})}


def usage_report(state: State) -> dict:
    """Tasks grouped by the model and effort of their first dispatch, with rounds per task."""
    groups: dict[str, dict] = {}
    rows = []
    for task in sorted(state.tasks()["tasks"].values(), key=lambda t: t["id"]):
        usage = task_usage(task)
        if not usage:
            continue
        first = task["dispatches"][0]
        label = "/".join(x for x in (first.get("model"), first.get("effort")) if x) or "unrecorded"
        rows.append({"id": task["id"], "status": task["status"], "model": label,
                     "rounds": usage["rounds"], **({"total": usage["total"]} if "total" in usage else {})})
        group = groups.setdefault(label, {"tasks": 0, "rounds": 0, "byStatus": {}, "total": {}})
        group["tasks"] += 1
        group["rounds"] += usage["rounds"]
        group["byStatus"][task["status"]] = group["byStatus"].get(task["status"], 0) + 1
        for key, value in usage.get("total", {}).items():
            if key != "contextPct":
                group["total"][key] = round(group["total"].get(key, 0) + value, 1)
    for group in groups.values():
        group["roundsPerTask"] = round(group["rounds"] / group["tasks"], 2)
        if not group["total"]:
            del group["total"]
    return {"groupedBy": "model and effort of each task's first dispatch",
            "note": "work done, not money: look prices up with the provider",
            "models": groups, "tasks": rows}

