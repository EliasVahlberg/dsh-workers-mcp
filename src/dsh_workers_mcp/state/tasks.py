"""
Tasks: the records, the statuses that the server sets as facts, and the
`dsh_tasks` tool.

`TasksMixin` adds the task methods to `State`. The functions that decide a
status from the data need no state directory.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from dsh_workers_mcp import dshctl
from dsh_workers_mcp import digest as digest_mod
from dsh_workers_mcp.state.store import Store, now_iso
from dsh_workers_mcp.state.usage import _minutes_between, task_usage, usage_delta, usage_report

if TYPE_CHECKING:
    from dsh_workers_mcp.state.core import State

PREVIEW_CHARS = 2_000       # dispatch prompt text kept in tasks.json


TASK_STATUSES = (
    "open",        # defined, not dispatched
    "dispatched",  # sent to a worker; its turn has not ended   (server-set)
    "blocked",     # the worker is waiting on a question/approval (server-set)
    "returned",    # the worker's turn ended; nobody has judged it (server-set)
    "in-review",   # the orchestrator is checking the result
    "merged",      # accepted and integrated
    "rejected",    # judged and not accepted
    "abandoned",   # dropped
    "done",        # finished without a merge: a decision taken, research delivered
)
# A task in a closed state is not touched by observations any more.
CLOSED = {"merged", "rejected", "abandoned", "done"}

def _next_status(task: dict, last: dict, end: dict, blocked: bool, unblocked: bool) -> str | None:
    """The status that a task moves to after an observation, or None when it stays."""
    seq_before = last.get("seqBefore")
    if end.get("seq", -1) > (seq_before if seq_before is not None else -1) and not blocked:
        return "returned"
    if blocked:
        return "blocked"
    if task["status"] == "blocked" and unblocked:
        return "dispatched"
    return None


def _record_return(last: dict, end: dict, state: dict) -> None:
    """Record on the dispatch how its turn ended and what it used."""
    last["turnEnd"] = end
    usage = usage_delta(last.get("usageBefore"), state.get("usage"))
    wall = _minutes_between(last.get("at"), end.get("at"))
    if wall is not None:
        usage["wallMinutes"] = wall
    if usage:
        last["usage"] = usage


def _record_transition(task: dict, new_status: str, end: dict) -> None:
    stamp = now_iso()
    task["status"] = new_status
    task["history"].append({"at": stamp, "status": new_status, "by": "server",
                            **({"turnEnd": end.get("kind")} if new_status == "returned" else {})})
    task["updated"] = stamp



class TasksMixin(Store):
    """The task records: add, update, dispatch and advance."""

    def add_task(self, title: str, owner: str | None = None,
                 acceptance: str | None = None, depends_on: list[str] | None = None,
                 branch: str | None = None, worktree: str | None = None,
                 task_id: str | None = None) -> dict:
        """Add an open task. `task_id` claims a specific id (e.g. one the user
        already named), so adds made in one batch cannot swap ids by arrival order."""
        if not title or not title.strip():
            raise dshctl.RpcError("a task needs a title")
        if task_id is not None and not re.fullmatch(r"T-\d{3,}", task_id):
            raise dshctl.RpcError(f"task id {task_id!r} must look like T-007")
        with self._lock("tasks"):
            data = self.tasks()
            for dep in depends_on or []:
                if dep not in data["tasks"]:
                    raise dshctl.RpcError(f"dependsOn: no task {dep!r}")
            if task_id is None:
                task_id = f"T-{data['nextId']:03d}"
            elif task_id in data["tasks"]:
                raise dshctl.RpcError(f"{task_id} already exists")
            data["nextId"] = max(data["nextId"], int(task_id[2:]) + 1)
            stamp = now_iso()
            data["tasks"][task_id] = {
                "id": task_id, "title": title.strip(), "status": "open",
                "owner": owner, "acceptance": acceptance,
                "dependsOn": depends_on or [], "branch": branch, "worktree": worktree,
                "created": stamp, "updated": stamp, "notes": [], "dispatches": [],
                "history": [{"at": stamp, "status": "open", "by": "orchestrator"}],
            }
            self._save_tasks(data)
            return data["tasks"][task_id]

    def update_task(self, task_id: str, status: str | None = None,
                    note: str | None = None, **fields) -> dict:
        allowed = {"title", "owner", "acceptance", "dependsOn", "branch", "worktree"}
        unknown = set(fields) - allowed
        if unknown:
            raise dshctl.RpcError(f"cannot set {sorted(unknown)}; settable: {sorted(allowed)}")
        if status is not None and status not in TASK_STATUSES:
            raise dshctl.RpcError(f"status {status!r} is not one of {list(TASK_STATUSES)}")
        with self._lock("tasks"):
            data = self.tasks()
            task = data["tasks"].get(task_id)
            if task is None:
                raise dshctl.RpcError(f"no task {task_id!r}")
            for dep in fields.get("dependsOn") or []:
                if dep not in data["tasks"] or dep == task_id:
                    raise dshctl.RpcError(f"dependsOn: invalid task {dep!r}")
            stamp = now_iso()
            for key, value in fields.items():
                if value is not None:
                    task[key] = value
            if status is not None and status != task["status"]:
                if status in CLOSED and not (note or "").strip():
                    raise dshctl.RpcError(
                        f"closing {task_id} as {status} needs a note: the commit for merged, "
                        "the reason for rejected or abandoned, the outcome for done")
                task["status"] = status
                task["history"].append({"at": stamp, "status": status, "by": "orchestrator"})
            if note:
                task["notes"].append({"at": stamp, "text": note})
            task["updated"] = stamp
            self._save_tasks(data)
            return task

    def check_dispatch(self, task_id: str, session_id: str) -> None:
        """Validate BEFORE the prompt is sent, so a bad task_id never dispatches work."""
        task = self.tasks()["tasks"].get(task_id)
        if task is None:
            raise dshctl.RpcError(f"no task {task_id!r} — create it with dsh_tasks action=add")
        # A rejection goes back to its worker (METHOD): dispatching it again IS the
        # reopen, and record_dispatch logs it. Merged and abandoned stay final.
        if task["status"] in CLOSED - {"rejected"}:
            raise dshctl.RpcError(f"{task_id} is {task['status']}; reopen it before dispatching")
        if task.get("owner") and task["owner"] != session_id:
            raise dshctl.RpcError(
                f"{task_id} is owned by {task['owner']}; reassign it with "
                f"dsh_tasks action=update owner=... first")

    def record_dispatch(self, task_id: str, session_id: str, prompt: str,
                        mode: str, seq_before: int | None,
                        facts: dict | None = None) -> dict:
        # Where the worktree stands now, so the return can say what changed. Taken
        # outside the lock: it runs git.
        known = self.tasks()["tasks"].get(task_id) or {}
        git_before = digest_mod.git_snapshot(known.get("worktree"))
        with self._lock("tasks"):
            data = self.tasks()
            task = data["tasks"][task_id]
            stamp = now_iso()
            task["owner"] = session_id
            task["dispatches"].append({
                "at": stamp, "sessionId": session_id, "mode": mode,
                "seqBefore": seq_before, "prompt": prompt[:PREVIEW_CHARS],
                **({"promptTruncated": True} if len(prompt) > PREVIEW_CHARS else {}),
                **({"gitBefore": git_before} if git_before else {}),
                **(facts or {}),
            })
            if task["status"] == "rejected":
                task["history"].append({"at": stamp, "status": "open", "by": "server",
                                        "note": "reopened by re-dispatch after rejection"})
            if task["status"] != "dispatched":
                task["status"] = "dispatched"
                task["history"].append({"at": stamp, "status": "dispatched", "by": "server"})
            task["updated"] = stamp
            self._save_tasks(data)
        self._update_worker(session_id, auto={"currentTask": task_id})
        return task

    def _advance_task(self, session_id: str, state: dict, blocked: bool,
                      unblocked: bool = False) -> None:
        """
        Server-side transitions, all factual: dispatched -> blocked when a wait
        found the worker waiting on an answer, blocked -> dispatched when that
        answer or approval is given here, and -> returned when a turn ends after
        the last dispatch. A plain status poll cannot see pending requests, so
        it never clears `blocked`. Judgment (in-review, merged, rejected) is
        left to the orchestrator.
        """
        worker = self.roster()["workers"].get(session_id) or {}
        task_id = (worker.get("auto") or {}).get("currentTask")
        if not task_id:
            return
        with self._lock("tasks"):
            data = self.tasks()
            task = data["tasks"].get(task_id)
            if task is None or task["status"] in CLOSED or not task["dispatches"]:
                return
            last = task["dispatches"][-1]
            end = state.get("lastTurnEnd") or {}
            new_status = _next_status(task, last, end, blocked, unblocked)
            if new_status == "returned":
                _record_return(last, end, state)
            if new_status and new_status != task["status"]:
                _record_transition(task, new_status, end)
                self._save_tasks(data)
            elif new_status == "returned":
                self._save_tasks(data)  # turnEnd recorded on the dispatch
        if new_status == "returned":
            self._update_worker(session_id, auto={"currentTask": None,
                                                  "lastTask": task_id})


def task_ack(task: dict) -> dict:
    """What a write returns: enough to confirm it, not the whole record
    (`dsh_tasks action=show` has the history). The caller just wrote the rest."""
    return {"id": task["id"], "title": task["title"], "status": task["status"],
            "owner": (task.get("owner") or "-").removeprefix("session-")[:8],
            "branch": task.get("branch"), "updated": task["updated"],
            "notes": len(task.get("notes", []))}


def cmd_tasks(state: State, arguments: dict) -> dict:
    action = arguments.get("action", "list")
    if action == "list":
        wanted = arguments.get("status")
        tasks = [
            {k: t.get(k) for k in ("id", "title", "status", "owner", "dependsOn",
                                   "branch", "updated")}
            for t in state.tasks()["tasks"].values()
            if (wanted is None or t["status"] == wanted)
            and (arguments.get("include_closed") or wanted or t["status"] not in CLOSED)
        ]
        return {"tasks": tasks}
    if action == "add":
        owner = arguments.get("owner")
        if owner:
            owner = dshctl.find_session(owner, state.client)["sessionId"]
        return task_ack(state.add_task(arguments.get("title", ""), owner=owner,
                                       acceptance=arguments.get("acceptance"),
                                       depends_on=arguments.get("depends_on"),
                                       branch=arguments.get("branch"),
                                       worktree=arguments.get("worktree"),
                                       task_id=arguments.get("id")))
    if action == "report":
        return usage_report(state)
    task_id = arguments.get("id")
    if not task_id:
        raise dshctl.RpcError(f"action {action!r} needs an id")
    if action == "show":
        task = state.tasks()["tasks"].get(task_id)
        if task is None:
            raise dshctl.RpcError(f"no task {task_id!r}")
        usage = task_usage(task)
        return {**task, "usage": usage} if usage else task
    if action == "update":
        owner = arguments.get("owner")
        if owner:
            owner = dshctl.find_session(owner, state.client)["sessionId"]
        return task_ack(state.update_task(
            task_id, status=arguments.get("status"), note=arguments.get("note"),
            title=arguments.get("title"), owner=owner,
            acceptance=arguments.get("acceptance"),
            dependsOn=arguments.get("depends_on"), branch=arguments.get("branch"),
            worktree=arguments.get("worktree")))
    raise dshctl.RpcError(f"unknown tasks action {action!r}")

