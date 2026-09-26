"""
Orchestrator state on disk: what the MCP server observes, written by the tool
so the orchestrator cannot forget to write it.

Principle: the server writes FACTS it observed; the orchestrator writes
JUDGMENT (roles, notes, task status beyond what a turn end implies), and only
through the dsh_roster / dsh_tasks tools, never by editing the files, so the
two authors cannot clobber each other.

Layout (all under one state directory; see resolve_state_dir):

    logs/YYYY-MM-DD.jsonl        every tools/call: arguments, result, duration
    history/<sessionId>/
        events.jsonl             append-only mirror of the dsh session log (no chunks)
        mirror.json              the mirror's lastSeq cursor
        state.json               "last observed" snapshot for catching up
    .locks/                      advisory locks for read-modify-write
    roster.json                  managed workers: `auto` (server) + `notes` (orchestrator)
    tasks.json                   tasks: structured fields + dispatch records

Everything cached here is "last observed", stamped with observedAt. While the
dsh server is up, the live tools stay the source of truth; this is what
survives the orchestrator's own session ending, and what can be audited
without dsh running.

Stdlib only, like the rest of the package.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

try:  # POSIX advisory locks; two orchestrator sessions may share a state dir
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: best effort, no locking
    fcntl = None  # type: ignore[assignment]

try:
    from dsh_workers_mcp import dshctl
except ImportError:  # pragma: no cover - script mode
    import dshctl  # type: ignore[no-redef]

DEFAULT_DIRNAME = ".orchestrator"

TASK_STATUSES = (
    "open",        # defined, not dispatched
    "dispatched",  # sent to a worker; its turn has not ended   (server-set)
    "blocked",     # the worker is waiting on a question/approval (server-set)
    "returned",    # the worker's turn ended; nobody has judged it (server-set)
    "in-review",   # the orchestrator is checking the result
    "merged",      # accepted and integrated
    "rejected",    # judged and not accepted
    "abandoned",   # dropped
)
# A task in a closed state is not touched by observations any more.
CLOSED = {"merged", "rejected", "abandoned"}

LOG_RESULT_CHARS = 20_000   # per logged call; the full text is in dsh anyway
PREVIEW_CHARS = 2_000       # prompt/reply text kept in state.json
HISTORY_PAGE_MESSAGES = 200
HISTORY_MAX_PAGES = 1_000   # first mirror of a long session walks it all once


# ------------------------------------------------------------------ helpers

def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ms_to_iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def resolve_state_dir(explicit: str | None, cwd: str | None = None) -> Path:
    """`--state-dir` when given, else <cwd>/.orchestrator (cwd = where the server runs)."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(cwd or os.getcwd()).resolve() / DEFAULT_DIRNAME


def _write_json(path: Path, data) -> None:
    """Atomic replace: a crash mid-write leaves the old file, never half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive advisory lock on `path` for a read-modify-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _user_prompt(event: dict) -> bool:
    """A real prompt, not a plugin-injected message (runtime context, approval notices)."""
    source = (event.get("data") or {}).get("source") or {}
    return event.get("type") == "user/message" and source.get("kind") == "user"


def short(session_id: str) -> str:
    return session_id.removeprefix("session-")[:8]


# -------------------------------------------------------------------- state

class State:
    """One orchestrator state directory. Created lazily on first write."""

    def __init__(self, root: Path | str, base: str = dshctl.DEFAULT_BASE):
        self.root = Path(root)
        self.base = base

    # paths
    @property
    def roster_path(self) -> Path:
        return self.root / "roster.json"

    @property
    def tasks_path(self) -> Path:
        return self.root / "tasks.json"

    def session_dir(self, session_id: str) -> Path:
        return self.root / "history" / session_id

    def _lock(self, name: str):
        return _locked(self.root / ".locks" / f"{name}.lock")

    # ------------------------------------------------------------ call log

    def log_call(self, tool: str, arguments: dict, text: str, ok: bool,
                 duration_ms: int, state_error: str | None = None) -> None:
        """Append one tools/call record. Never raises into the caller's path."""
        record = {
            "at": now_iso(), "tool": tool, "arguments": arguments, "ok": ok,
            "durationMs": duration_ms, "resultChars": len(text),
            "result": text[:LOG_RESULT_CHARS],
            **({"resultTruncated": True} if len(text) > LOG_RESULT_CHARS else {}),
            **({"stateError": state_error} if state_error else {}),
        }
        path = self.root / "logs" / f"{_dt.date.today().isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # One write() on an O_APPEND file: lines from concurrent servers do not interleave
        # for records this size on Linux.
        with open(path, "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # --------------------------------------------------------------- roster

    def roster(self) -> dict:
        return _read_json(self.roster_path, {"version": 1, "workers": {}})

    def is_managed(self, session_id: str) -> bool:
        return session_id in self.roster()["workers"]

    def _update_worker(self, session_id: str, auto: dict | None = None,
                       notes: dict | None = None, create: bool = False) -> dict:
        with self._lock("roster"):
            roster = self.roster()
            workers = roster["workers"]
            if session_id not in workers:
                if not create:
                    raise dshctl.RpcError(
                        f"{session_id} is not on the roster — adopt it first "
                        f"(dsh_roster action=adopt)")
                workers[session_id] = {
                    "auto": {"sessionId": session_id, "short": short(session_id),
                             "addedAt": now_iso()},
                    "notes": {"role": None, "retired": False, "log": []},
                }
            worker = workers[session_id]
            if auto:
                worker["auto"].update(auto)
                worker["auto"]["observedAt"] = now_iso()
            if notes:
                worker["notes"].update(notes)
            _write_json(self.roster_path, roster)
            return worker

    def adopt(self, session_id: str, how: str, role: str | None = None) -> dict:
        """Put a session on the roster. Idempotent; `how` records why it is managed."""
        existing = self.roster()["workers"].get(session_id)
        auto = {} if existing else {"addedBy": how}
        worker = self._update_worker(session_id, auto=auto, create=True)
        if role:
            worker = self._update_worker(session_id, notes={"role": role})
        return worker

    def add_note(self, session_id: str, text: str) -> dict:
        with self._lock("roster"):
            roster = self.roster()
            if session_id not in roster["workers"]:
                raise dshctl.RpcError(f"{session_id} is not on the roster — adopt it first")
            roster["workers"][session_id]["notes"]["log"].append({"at": now_iso(), "text": text})
            _write_json(self.roster_path, roster)
            return roster["workers"][session_id]

    def set_judgment(self, session_id: str, role: str | None = None,
                     retired: bool | None = None) -> dict:
        notes = {}
        if role is not None:
            notes["role"] = role
        if retired is not None:
            notes["retired"] = retired
        if not notes:
            raise dshctl.RpcError("nothing to set: pass role and/or retired")
        return self._update_worker(session_id, notes=notes)

    def reconcile(self, summaries: list[dict]) -> dict[str, dict]:
        """
        Refresh auto fields from a session.list and report drift per managed worker.

        Only calls made through this server are captured, so anything done in
        the dsh UI (rename, preset switch, deleting the session) is invisible
        until reconciled. Drift is reported, never silently absorbed.
        """
        by_id = {s["sessionId"]: s for s in summaries}
        report: dict[str, dict] = {}
        for session_id, worker in self.roster()["workers"].items():
            if worker["notes"].get("retired"):
                continue
            summary = by_id.get(session_id)
            drift = []
            if summary is None:
                self._update_worker(session_id, auto={"missing": True})
                report[session_id] = {"missing": True,
                                      "drift": ["session no longer exists in dsh"]}
                continue
            values = dshctl.projections(summary)
            title = values.get("title")
            preset = (values.get("permissions") or {}).get("currentValue")
            auto = worker["auto"]
            if auto.get("name") and title != auto["name"]:
                drift.append(f"title {title!r} differs from the name set here {auto['name']!r}")
            if auto.get("permissionSet") and preset != auto["permissionSet"]:
                drift.append(f"permission {preset!r} differs from the one set here "
                             f"{auto['permissionSet']!r}")
            self._update_worker(session_id, auto={
                "title": title, "permission": preset, "cwd": summary.get("cwd"),
                "agentPreset": summary.get("agentPreset"),
                "running": bool(summary.get("running")), "missing": False,
            })
            report[session_id] = {"role": worker["notes"].get("role"),
                                  **({"drift": drift} if drift else {})}
        return report

    # -------------------------------------------------------------- history

    def sync_history(self, session_id: str) -> dict:
        """
        Append events newer than the cursor to history/<id>/events.jsonl.

        dsh history only pages backwards (beforeSeq), so this walks back from
        the tail until it reaches the cursor. Streaming chunks are dropped (the
        committed assistant/message carries the same text), which also keeps
        the tail page's in-flight partial out of the mirror. Returns the new
        events, oldest first.
        """
        sdir = self.session_dir(session_id)
        with self._lock(f"history-{session_id}"):
            meta = _read_json(sdir / "mirror.json", {"lastSeq": -1})
            cursor = meta["lastSeq"]
            collected: list[dict] = []
            before: int | None = None
            complete = False
            for _ in range(HISTORY_MAX_PAGES):
                payload: dict = {"sessionId": session_id,
                                 "maxMessages": HISTORY_PAGE_MESSAGES}
                if before is not None:
                    payload["beforeSeq"] = before
                value = dshctl.rpc("session.history", payload, base=self.base)
                raw = [e.get("event", e) for e in value.get("events", [])]
                if not raw:
                    complete = True
                    break
                collected = [e for e in raw if e.get("seq", -1) > cursor
                             and e.get("type") not in dshctl.NOISE_TYPES] + collected
                lowest = min(e.get("seq", 0) for e in raw)
                if lowest <= cursor + 1 or not value.get("hasMore") or lowest <= 0:
                    complete = True
                    break
                before = lowest
            if not complete:
                raise dshctl.RpcError(
                    f"history mirror for {session_id} did not reach its cursor within "
                    f"{HISTORY_MAX_PAGES} pages; nothing written, so no gap is created")
            collected.sort(key=lambda e: e.get("seq", -1))
            if collected:
                sdir.mkdir(parents=True, exist_ok=True)
                with open(sdir / "events.jsonl", "a") as fh:
                    for event in collected:
                        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                meta = {"lastSeq": collected[-1]["seq"], "syncedAt": now_iso()}
                _write_json(sdir / "mirror.json", meta)
            return {"new": collected, "lastSeq": meta["lastSeq"]}

    # ---------------------------------------------------------------- state

    def session_state(self, session_id: str) -> dict:
        return _read_json(self.session_dir(session_id) / "state.json", {})

    def observe(self, session_id: str, summary: dict | None = None,
                blocked: list | None = None, unblocked: bool = False) -> dict:
        """
        Mirror new history, refresh state.json and the roster's auto fields,
        and advance the worker's current task on a turn end. For managed
        sessions only.
        """
        if not self.is_managed(session_id):
            return {}
        synced = self.sync_history(session_id)
        summary = summary or dshctl.find_session(session_id, self.base)
        values = dshctl.projections(summary)
        stats = values.get("sessionStats") or {}
        pressure = values.get("contextPressure") or {}
        window = pressure.get("contextWindow") or 0
        used = pressure.get("pressureTokens") or 0
        goal = (values.get("goal") or {}).get("goal") or {}

        prev = self.session_state(session_id)
        state = dict(prev)
        for event in synced["new"]:
            etype = event.get("type")
            when = ms_to_iso(event.get("time"))
            if _user_prompt(event):
                state["lastPrompt"] = {"seq": event["seq"], "at": when,
                                       "text": dshctl.text_of(event)[:PREVIEW_CHARS]}
            elif etype == "assistant/message":
                state["lastReply"] = {"seq": event["seq"], "at": when,
                                      "text": dshctl.text_of(event)[:PREVIEW_CHARS]}
            elif etype == "turn/end":
                reason = (event.get("data") or {}).get("reason") or {}
                state["lastTurnEnd"] = {"seq": event["seq"], "at": when, **reason}
        running = bool(summary.get("running"))
        state.update({
            "sessionId": session_id,
            "observedAt": now_iso(),
            "running": running,
            "title": values.get("title"),
            "permission": (values.get("permissions") or {}).get("currentValue"),
            "cwd": summary.get("cwd"),
            "goal": {"objective": goal.get("objective"), "status": goal.get("status")}
            if goal else None,
            "context": {"window": window, "used": used,
                        "usedPct": round(used / window * 100, 1) if window else None},
            "work": {"turns": stats.get("turns"), "steps": stats.get("steps")},
            "mirroredSeq": synced["lastSeq"],
        })
        if blocked:
            state["blocked"] = {"at": now_iso(), "pending": [
                {k: item.get(k) for k in ("kind", "rpcId", "toolName", "reason")
                 if item.get(k) is not None} for item in blocked]}
        elif unblocked or not running or state.get("lastTurnEnd", {}).get("seq", -1) > \
                prev.get("lastTurnEnd", {}).get("seq", -1):
            state.pop("blocked", None)
        _write_json(self.session_dir(session_id) / "state.json", state)

        self._update_worker(session_id, auto={
            "title": state["title"], "permission": state["permission"],
            "cwd": state["cwd"], "agentPreset": summary.get("agentPreset"),
            "running": running, "goal": state["goal"],
            "lastTurnEnd": state.get("lastTurnEnd"), "missing": False,
        })
        self._advance_task(session_id, state, blocked=bool(blocked), unblocked=unblocked)
        return state

    # ---------------------------------------------------------------- tasks

    def tasks(self) -> dict:
        return _read_json(self.tasks_path, {"version": 1, "nextId": 1, "tasks": {}})

    def _save_tasks(self, data: dict) -> None:
        _write_json(self.tasks_path, data)

    def add_task(self, title: str, owner: str | None = None,
                 acceptance: str | None = None, depends_on: list[str] | None = None,
                 branch: str | None = None, worktree: str | None = None) -> dict:
        if not title or not title.strip():
            raise dshctl.RpcError("a task needs a title")
        with self._lock("tasks"):
            data = self.tasks()
            for dep in depends_on or []:
                if dep not in data["tasks"]:
                    raise dshctl.RpcError(f"dependsOn: no task {dep!r}")
            task_id = f"T-{data['nextId']:03d}"
            data["nextId"] += 1
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
        if task["status"] in CLOSED:
            raise dshctl.RpcError(f"{task_id} is {task['status']}; reopen it before dispatching")
        if task.get("owner") and task["owner"] != session_id:
            raise dshctl.RpcError(
                f"{task_id} is owned by {task['owner']}; reassign it with "
                f"dsh_tasks action=update owner=... first")

    def record_dispatch(self, task_id: str, session_id: str, prompt: str,
                        mode: str, seq_before: int | None) -> dict:
        with self._lock("tasks"):
            data = self.tasks()
            task = data["tasks"][task_id]
            stamp = now_iso()
            task["owner"] = session_id
            task["dispatches"].append({
                "at": stamp, "sessionId": session_id, "mode": mode,
                "seqBefore": seq_before, "prompt": prompt[:PREVIEW_CHARS],
                **({"promptTruncated": True} if len(prompt) > PREVIEW_CHARS else {}),
            })
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
            stamp = now_iso()
            new_status = None
            if end.get("seq", -1) > (last.get("seqBefore") if last.get("seqBefore")
                                     is not None else -1) and not blocked:
                last["turnEnd"] = end
                new_status = "returned"
            elif blocked:
                new_status = "blocked"
            elif task["status"] == "blocked" and unblocked:
                new_status = "dispatched"
            if new_status and new_status != task["status"]:
                task["status"] = new_status
                task["history"].append({"at": stamp, "status": new_status, "by": "server",
                                        **({"turnEnd": end.get("kind")}
                                           if new_status == "returned" else {})})
                task["updated"] = stamp
                self._save_tasks(data)
            elif new_status == "returned":
                self._save_tasks(data)  # turnEnd recorded on the dispatch
        if new_status == "returned":
            self._update_worker(session_id, auto={"currentTask": None,
                                                  "lastTask": task_id})


# --------------------------------------------------------- tool-level hooks

def _session_of(arguments: dict, result: object, base: str) -> str | None:
    if isinstance(result, dict) and isinstance(result.get("sessionId"), str):
        return result["sessionId"]
    ref = arguments.get("session")
    if not ref:
        return None
    return dshctl.find_session(ref, base)["sessionId"]


# Tools whose call means "this session is doing work for us": adopt it.
ADOPTING = {"dsh_create", "dsh_task", "dsh_ask"}
# Tools after which a managed session is re-observed.
OBSERVING = {"dsh_create", "dsh_task", "dsh_ask", "dsh_wait", "dsh_status",
             "dsh_name", "dsh_permission", "dsh_goal", "dsh_cancel",
             "dsh_answer", "dsh_approve"}


def before_call(state: State, tool: str, arguments: dict) -> None:
    """Validation that must happen before a tool acts. Raises to refuse the call."""
    if tool == "dsh_task" and arguments.get("task_id"):
        session_id = dshctl.find_session(arguments["session"], state.base)["sessionId"]
        state.check_dispatch(arguments["task_id"], session_id)


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
        summaries = dshctl.rpc("session.list", base=state.base).get("items", [])
        report = state.reconcile(summaries)
        listed = {row["sessionId"] for row in result}
        for row in result:
            if row["sessionId"] in report:
                row["roster"] = report[row["sessionId"]]
        for session_id, entry in report.items():
            if session_id not in listed and entry.get("missing"):
                result.append({"sessionId": session_id, "roster": entry})
        return result

    if tool not in OBSERVING:
        return None
    session_id = _session_of(arguments, result, state.base)
    if not session_id:
        return None

    if tool in ADOPTING:
        state.adopt(session_id, how=tool)
    if not state.is_managed(session_id):
        return None

    auto: dict = {}
    if tool == "dsh_create" and isinstance(result, dict):
        auto["createdAt"] = now_iso()
        if result.get("selectedModel"):
            auto["model"] = result["selectedModel"]
        else:
            with contextlib.suppress(dshctl.RpcError):
                current = dshctl.rpc("session.models", {"sessionId": session_id},
                                     base=state.base).get("current") or {}
                if current:
                    auto["model"] = f"{current.get('provider')}/{current.get('model')}"
        if arguments.get("permission"):
            auto["permissionSet"] = arguments["permission"]
    elif tool == "dsh_name" and isinstance(result, dict):
        auto["name"] = result.get("title")
    elif tool == "dsh_permission" and arguments.get("preset"):
        auto["permissionSet"] = arguments["preset"]
    if auto:
        state._update_worker(session_id, auto=auto)

    if tool == "dsh_task" and arguments.get("task_id") and isinstance(result, dict):
        state.record_dispatch(arguments["task_id"], session_id, arguments.get("prompt", ""),
                              arguments.get("mode", "queue"), result.get("seqBefore"))

    blocked = result.get("pending") if isinstance(result, dict) and result.get("blocked") else None
    state.observe(session_id, blocked=blocked,
                  unblocked=tool in ("dsh_answer", "dsh_approve"))
    return None


# --------------------------------------------------------- tool implementations

def roster_view(state: State, include_retired: bool = False) -> dict:
    workers = []
    for session_id, worker in state.roster()["workers"].items():
        if worker["notes"].get("retired") and not include_retired:
            continue
        auto, notes = worker["auto"], worker["notes"]
        workers.append({
            "sessionId": session_id, "short": auto.get("short"),
            "role": notes.get("role"), "title": auto.get("title"),
            "permission": auto.get("permission"), "model": auto.get("model"),
            "running": auto.get("running"), "currentTask": auto.get("currentTask"),
            "lastTurnEnd": (auto.get("lastTurnEnd") or {}).get("kind"),
            "observedAt": auto.get("observedAt"),
            **({"missing": True} if auto.get("missing") else {}),
            **({"retired": True} if notes.get("retired") else {}),
            "notes": notes.get("log", [])[-3:],
        })
    return {"stateDir": str(state.root), "workers": workers}


def cmd_roster(state: State, arguments: dict) -> dict:
    action = arguments.get("action", "show")
    if action == "show":
        if arguments.get("session"):
            session_id = dshctl.find_session(arguments["session"], state.base)["sessionId"]
            worker = state.roster()["workers"].get(session_id)
            if worker is None:
                raise dshctl.RpcError(f"{session_id} is not on the roster")
            return {"worker": worker, "state": state.session_state(session_id)}
        return roster_view(state, arguments.get("include_retired", False))
    if not arguments.get("session"):
        raise dshctl.RpcError(f"action {action!r} needs a session")
    session_id = dshctl.find_session(arguments["session"], state.base)["sessionId"]
    if action == "adopt":
        state.adopt(session_id, how="adopt", role=arguments.get("role"))
        state.observe(session_id)
        return state.roster()["workers"][session_id]
    if action == "note":
        if not arguments.get("text"):
            raise dshctl.RpcError("note needs text")
        return state.add_note(session_id, arguments["text"])
    if action == "set":
        return state.set_judgment(session_id, role=arguments.get("role"),
                                  retired=arguments.get("retired"))
    raise dshctl.RpcError(f"unknown roster action {action!r}")


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
            owner = dshctl.find_session(owner, state.base)["sessionId"]
        return state.add_task(arguments.get("title", ""), owner=owner,
                              acceptance=arguments.get("acceptance"),
                              depends_on=arguments.get("depends_on"),
                              branch=arguments.get("branch"),
                              worktree=arguments.get("worktree"))
    task_id = arguments.get("id")
    if not task_id:
        raise dshctl.RpcError(f"action {action!r} needs an id")
    if action == "show":
        task = state.tasks()["tasks"].get(task_id)
        if task is None:
            raise dshctl.RpcError(f"no task {task_id!r}")
        return task
    if action == "update":
        owner = arguments.get("owner")
        if owner:
            owner = dshctl.find_session(owner, state.base)["sessionId"]
        return state.update_task(
            task_id, status=arguments.get("status"), note=arguments.get("note"),
            title=arguments.get("title"), owner=owner,
            acceptance=arguments.get("acceptance"),
            dependsOn=arguments.get("depends_on"), branch=arguments.get("branch"),
            worktree=arguments.get("worktree"))
    raise dshctl.RpcError(f"unknown tasks action {action!r}")

