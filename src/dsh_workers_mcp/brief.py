"""
dsh_brief: the start-of-session sweep as one call.

The method says to sweep at the start of a lead session, after a compaction and
after a long wait: the roster, the tasks, what is pending, who is idle. That was
four calls the lead joined by hand. This joins them and, more usefully, derives
the to-do list: what needs the lead's action, in one line each.

`build` is pure over the state directory (plus pending requests handed in), so
it is testable with fixture state. `cmd_brief` adds the live refresh, which is
skipped (and said so) when dsh is down.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import subprocess
from pathlib import Path

try:
    from dsh_workers_mcp import digest, dshctl, state as st
except ImportError:  # pragma: no cover - script mode
    import digest  # type: ignore[no-redef]
    import dshctl  # type: ignore[no-redef]
    import state as st  # type: ignore[no-redef]

HOOK_NAME = "brief-extra"       # optional executable in the state dir
HOOK_TIMEOUT = 10
HOOK_MAX_LINES = 20
HOOK_LINE_CHARS = 200
TITLE_CHARS = 60
ROLE_CHARS = 40
# A note that starts like this marks work that waits on something else, with the
# first action to take on resume (PLAYBOOKS §5). The brief shows it on the task.
PARKED = "PARKED on"
PARKED_CHARS = 160
# Statuses in which the worktree is being worked on or looked at: show its size.
WORKING = {"dispatched", "blocked", "returned", "in-review"}


def age(iso: str | None, now: _dt.datetime) -> str:
    """'5m', '3h', '2d': how long ago. '?' when unknown."""
    if not iso:
        return "?"
    try:
        seconds = (now - _dt.datetime.fromisoformat(iso)).total_seconds()
    except (ValueError, TypeError):
        return "?"
    seconds = max(seconds, 0)
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _role(notes: dict) -> str:
    """The role's first clause: roles are free text and can run to a paragraph."""
    role = (notes.get("role") or "(no role)").split(" \u2014 ")[0].split(". ")[0]
    return role if len(role) <= ROLE_CHARS else role[: ROLE_CHARS - 1] + "\u2026"


def _short(session_id: str | None) -> str:
    return (session_id or "-").removeprefix("session-")[:8]


def _size(task: dict) -> str | None:
    """The task's worktree against the state it had when last dispatched."""
    dispatches = [d for d in task.get("dispatches", []) if d.get("gitBefore")]
    if not dispatches:
        return None
    changes = digest.git_changes(task.get("worktree"), dispatches[-1]["gitBefore"])
    if not changes:
        return None
    parts = [f"{changes['commits']} commit(s)" if changes.get("commits") else None,
             f"{changes['filesChanged']} file(s)",
             f"+{changes.get('insertions', 0)} -{changes.get('deletions', 0)}",
             f"{changes['uncommitted']} uncommitted"]
    return ", ".join(p for p in parts if p)


def build(state: st.State, *, now: _dt.datetime | None = None,
          pending: list[dict] | None = None, dsh: str = "not checked") -> dict:
    now = now or _dt.datetime.now().astimezone()
    needs: list[str] = []

    # ---- workers
    workers, active = [], {}
    for session_id, worker in state.roster()["workers"].items():
        if worker["notes"].get("retired"):
            continue
        active[session_id] = worker
        auto, notes = worker["auto"], worker["notes"]
        context = state.session_state(session_id).get("context") or {}
        pct = context.get("usedPct")
        status = "MISSING" if auto.get("missing") else "running" if auto.get("running") else "idle"
        end = (auto.get("lastTurnEnd") or {}).get("kind")
        workers.append(" | ".join([
            f"{auto.get('short') or _short(session_id)} {_role(notes)}",
            status, f"task {auto.get('currentTask') or '-'}",
            f"last turn {end or '-'}" + (f" {age((auto.get('lastTurnEnd') or {}).get('at'), now)} ago"
                                        if end else ""),
            *([f"ctx {pct:.0f}%"] if pct is not None else [])]))
        if auto.get("missing"):
            needs.append(f"{_short(session_id)}: session no longer exists in dsh")
        elif auto.get("compactionStalled"):
            needs.append(f"{_short(session_id)}: compaction looks stalled: "
                         f"{auto['compactionStalled']}")
        elif not auto.get("running") and not auto.get("currentTask"):
            needs.append(f"work for {_short(session_id)} ({_role(notes)}): "
                         f"idle, no task")

    # ---- tasks
    tasks = []
    open_tasks = [t for t in state.tasks()["tasks"].values() if t["status"] not in st.CLOSED]
    for task in sorted(open_tasks, key=lambda t: t["id"]):
        since = age(task.get("updated"), now)
        line = (f"{task['id']} [{task['status']} {since}] {task['title'][:TITLE_CHARS]} "
                f"(owner {_short(task.get('owner'))})")
        size = _size(task) if task["status"] in WORKING else None
        usage = st.task_usage(task)
        used = None
        if usage and usage.get("total"):
            total = usage["total"]
            used = (f"{usage['rounds']} round(s)"
                    + (f", {', '.join(usage['models'])}" if usage.get("models") else "")
                    + (f", out {total['output']:,}" if "output" in total else "")
                    + (f", {total['steps']:g} steps" if "steps" in total else ""))
        tasks.append(line + "".join(f" | {part}" for part in (size, used) if part))
        notes = task.get("notes") or []
        parked = notes[-1]["text"] if notes and notes[-1]["text"].startswith(PARKED) else None
        if parked:
            tasks[-1] += f" | {parked[:PARKED_CHARS]}"
        if task["status"] == "returned":
            needs.append(f"verdict on {task['id']}: returned {since} ago")
        elif task["status"] == "blocked":
            needs.append(f"answer for {task['id']}: blocked {since}")
        elif task["status"] == "in-review":
            needs.append(f"{task['id']} has been in-review for {since}: finish or hand back")

    # ---- what dsh is waiting on
    for item in pending or []:
        who = _short(item.get("sessionId"))
        what = item.get("toolName") or ("question" if item.get("kind") == "question/requested"
                                        else item.get("kind"))
        needs.append(f"pending from {who}: {what} (rpc_id {item.get('rpcId')})")

    return {"dsh": dsh, "needsAction": needs, "workers": workers, "openTasks": tasks}


def run_hook(state: st.State) -> list[str] | None:
    """Project-specific lines: `<state dir>/brief-extra`, if it exists and is executable.

    The project's own script, trusted like the rest of its state directory. It
    gets the state directory as $ORCHESTRATOR_DIR and runs from the directory
    above it. Its output is capped; a failure is reported as a line, not raised.
    """
    hook = state.root / HOOK_NAME
    if not (hook.is_file() and os.access(hook, os.X_OK)):
        return None
    try:
        result = subprocess.run([str(hook)], cwd=state.root.parent, capture_output=True,
                                text=True, timeout=HOOK_TIMEOUT,
                                env={**os.environ, "ORCHESTRATOR_DIR": str(state.root)})
    except (subprocess.TimeoutExpired, OSError) as exc:
        return [f"{HOOK_NAME} failed: {exc.__class__.__name__}"]
    lines = [line[:HOOK_LINE_CHARS] for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0:
        lines.append(f"{HOOK_NAME} exited {result.returncode}")
    return lines[:HOOK_MAX_LINES] + ([f"+{len(lines) - HOOK_MAX_LINES} more"]
                                     if len(lines) > HOOK_MAX_LINES else [])


def cmd_brief(state: st.State, arguments: dict) -> dict:
    live = arguments.get("live", True)
    pending: list[dict] | None = None
    dsh = "not checked (live=false)"
    drift: list[str] = []
    if live:
        try:
            summaries = dshctl.rpc("session.list", base=state.base).get("items", [])
            dsh = "up"
            by_id = {s["sessionId"]: s for s in summaries}
            for session_id, report in state.reconcile(summaries).items():
                drift += [f"{_short(session_id)}: {d}" for d in report.get("drift", [])]
            for session_id, worker in state.roster()["workers"].items():
                if session_id in by_id and not worker["notes"].get("retired"):
                    with contextlib.suppress(dshctl.RpcError):
                        state.observe(session_id, by_id[session_id])
            with contextlib.suppress(dshctl.RpcError, OSError):
                pending = dshctl.pending_requests(state.base, 1.5, 8.0)
        except (dshctl.RpcError, OSError):
            dsh = "unreachable: showing the last observed state"
    result = build(state, pending=pending, dsh=dsh)
    result["needsAction"] += drift
    if live and pending is None and dsh == "up":
        result["needsAction"].append("could not read dsh_pending: call it yourself")
    extra = run_hook(state)
    if extra is not None:
        result["project"] = extra
    return result
