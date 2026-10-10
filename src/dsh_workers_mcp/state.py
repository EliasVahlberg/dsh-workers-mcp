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
import re
import os
import tempfile
from typing import Any
from pathlib import Path

try:  # POSIX advisory locks; two orchestrator sessions may share a state dir
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: best effort, no locking
    fcntl = None  # type: ignore[assignment]

from dsh_workers_mcp import digest as digest_mod
from dsh_workers_mcp import dshctl
from dsh_workers_mcp.transport import DshClient, HttpClient

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
    "done",        # finished without a merge: a decision taken, research delivered
)
# A task in a closed state is not touched by observations any more.
CLOSED = {"merged", "rejected", "abandoned", "done"}

# --- compaction watch -------------------------------------------------------
# dsh compacts a session itself when it reaches `thresholdRatio` of its window
# (default 0.8). The one real failure is a compaction that does not run (an
# orphaned lock), after which the context keeps filling. These are OUR
# heuristics for noticing that from turn-end observations; they are not dsh
# settings, and a deployment that changed thresholdRatio should move them.
COMPACT_PCT = 80.0       # where dsh is expected to compact (its default)
STALL_PCT = 85.0         # still above this at a turn end: compaction did not run
CLIMB_TURNS = 3          # turn ends at/above COMPACT_PCT, rising, with no summary
COMPACTION_HISTORY = 12  # turn-end observations kept per worker


def compaction_stalled(comp: dict) -> str | None:
    """
    Why a worker's compaction looks stalled, or None. `comp` is the per-worker
    record: `summaries` (count of compaction/summary events seen; count those,
    because start/end brackets can be no-ops) and `history` (usedPct and the
    summary count at each recent turn end).
    """
    history = comp.get("history") or []
    if not history:
        return None
    last = history[-1]
    if comp.get("summaries", 0) > last["summaries"]:
        return None  # a summary landed after that turn end: it ran
    if last["usedPct"] > STALL_PCT:
        return (f"context was {last['usedPct']:.0f}% at the last turn end; dsh compacts "
                f"at about {COMPACT_PCT:.0f}% (a stall threshold of {STALL_PCT:.0f}% is a heuristic)")
    tail = history[-CLIMB_TURNS:]
    if (len(tail) == CLIMB_TURNS
            and all(h["usedPct"] >= COMPACT_PCT for h in tail)
            and len({h["summaries"] for h in tail}) == 1
            and all(a["usedPct"] < b["usedPct"] for a, b in zip(tail, tail[1:], strict=False))):
        levels = " -> ".join(f"{h['usedPct']:.0f}%" for h in tail)
        return f"context rose {levels} over {CLIMB_TURNS} turn ends with no compaction summary"
    return None

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


def _fold_new_events(state: dict, new_events: list[dict]) -> None:
    """Update lastPrompt, lastReply and lastTurnEnd from the events that were just mirrored."""
    for event in new_events:
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


def _observed_state(prev: dict, summary: dict, new_events: list[dict], last_seq: int,
                    session_id: str) -> dict:
    """The new state.json of a session: the previous one, the new events and the dsh summary."""
    state = dict(prev)
    _fold_new_events(state, new_events)
    values = dshctl.projections(summary)
    stats = values.get("sessionStats") or {}
    pressure = values.get("contextPressure") or {}
    window = pressure.get("contextWindow") or 0
    used = pressure.get("pressureTokens") or 0
    goal = (values.get("goal") or {}).get("goal") or {}
    state.update({
        "sessionId": session_id,
        "observedAt": now_iso(),
        "running": bool(summary.get("running")),
        "title": values.get("title"),
        "permission": (values.get("permissions") or {}).get("currentValue"),
        "cwd": summary.get("cwd"),
        "goal": {"objective": goal.get("objective"), "status": goal.get("status")}
        if goal else None,
        "context": {"window": window, "used": used,
                    "usedPct": round(used / window * 100, 1) if window else None},
        "work": {"turns": stats.get("turns"), "steps": stats.get("steps")},
        "mirroredSeq": last_seq,
    })
    return state


def _update_blocked(state: dict, prev: dict, blocked: list | None, unblocked: bool) -> None:
    """Set `blocked` while requests are pending. Clear it once the worker is unblocked or moves on."""
    if blocked:
        state["blocked"] = {"at": now_iso(), "pending": [
            {k: item.get(k) for k in ("kind", "rpcId", "toolName", "reason")
             if item.get(k) is not None} for item in blocked]}
        return
    ended_since = state.get("lastTurnEnd", {}).get("seq", -1) > prev.get("lastTurnEnd", {}).get("seq", -1)
    if unblocked or not state["running"] or ended_since:
        state.pop("blocked", None)


def _worker_auto(state: dict, summary: dict) -> dict:
    """The roster's auto fields for a worker, from its new state and the dsh summary."""
    return {
        "title": state["title"], "permission": state["permission"],
        "cwd": state["cwd"], "agentPreset": summary.get("agentPreset"),
        "running": state["running"], "goal": state["goal"],
        "lastTurnEnd": state.get("lastTurnEnd"), "missing": False,
        "compactionStalled": state["compaction"].get("stalled"),
    }


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


# -------------------------------------------------------------------- state

class State:
    """One orchestrator state directory. Created lazily on first write."""

    def __init__(self, root: Path | str, client: DshClient | None = None):
        self.root = Path(root)
        self.client: DshClient = client or HttpClient()

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
        notes: dict[str, Any] = {}
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
                                  **({"drift": drift} if drift else {}),
                                  **({"compactionStalled": auto["compactionStalled"]}
                                     if auto.get("compactionStalled") else {})}
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
                value = self.client.rpc("session.history", payload)
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
        summary = summary or dshctl.find_session(session_id, self.client)
        prev = self.session_state(session_id)
        state = _observed_state(prev, summary, synced["new"], synced["lastSeq"], session_id)
        state["compaction"] = self._track_compaction(session_id, prev, state, synced["new"])
        state["usage"] = usage_snapshot(summary)
        _update_blocked(state, prev, blocked, unblocked)
        _write_json(self.session_dir(session_id) / "state.json", state)
        self._update_worker(session_id, auto=_worker_auto(state, summary))
        self._advance_task(session_id, state, blocked=bool(blocked), unblocked=unblocked)
        return state

    def _track_compaction(self, session_id: str, prev: dict, state: dict,
                          new_events: list[dict]) -> dict:
        """
        Count the compaction summaries seen and keep a short history of the
        context level at each turn end, then judge it (see compaction_stalled).
        """
        comp = dict(prev.get("compaction") or {})
        if "summaries" not in comp:   # first look at this session: count what is mirrored
            path = self.session_dir(session_id) / "events.jsonl"
            comp["summaries"] = sum(
                1 for line in path.open(encoding="utf-8") if '"compaction/summary"' in line
            ) if path.exists() else 0
        else:
            comp["summaries"] += sum(1 for e in new_events if e.get("type") == "compaction/summary")
        end = state.get("lastTurnEnd") or {}
        pct = (state.get("context") or {}).get("usedPct")
        # A level read while a turn is running is not "at a turn end".
        if end.get("seq", -1) > comp.get("lastSeq", -1) and not state.get("running") \
                and pct is not None:
            history = (comp.get("history") or []) + [
                {"seq": end["seq"], "usedPct": pct, "summaries": comp["summaries"]}]
            comp["history"] = history[-COMPACTION_HISTORY:]
            comp["lastSeq"] = end["seq"]
        reason = compaction_stalled(comp)
        if reason:
            comp["stalled"] = reason
        else:
            comp.pop("stalled", None)
        return comp

    # ---------------------------------------------------------------- tasks

    def tasks(self) -> dict:
        return _read_json(self.tasks_path, {"version": 1, "nextId": 1, "tasks": {}})

    def _save_tasks(self, data: dict) -> None:
        _write_json(self.tasks_path, data)

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


# --------------------------------------------------------- tool-level hooks

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


def _mirrored_events(state: State, session_id: str) -> list[dict]:
    path = state.session_dir(session_id) / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _check_patterns(state: State) -> tuple[list[str], list[str]]:
    """The project's extra check regexes from `checks.txt`, as (valid, invalid)."""
    patterns: list[str] = []
    bad: list[str] = []
    checks_file = state.root / "checks.txt"
    lines = checks_file.read_text().splitlines() if checks_file.exists() else []
    for line in (raw.strip() for raw in lines):
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
            patterns.append(line)
        except re.error:
            bad.append(line)
    return patterns, bad


def _digest_task(state: State, session_id: str, task_id: str | None) -> dict | None:
    """The task that a digest is about: the one named, else the worker's current or last."""
    auto = (state.roster()["workers"].get(session_id) or {}).get("auto", {})
    task_id = task_id or auto.get("currentTask") or auto.get("lastTask")
    if not task_id:
        return None
    task = state.tasks()["tasks"].get(task_id)
    if task is None:
        raise dshctl.RpcError(f"no task {task_id!r}")
    return task


def digest_for(state: State, session_id: str, turns: int = 1,
               task_id: str | None = None, observe: bool = True) -> dict:
    """The digest of a managed worker's last `turns` turns (see digest.py)."""
    if not state.is_managed(session_id):
        raise dshctl.RpcError(f"{session_id} is not on the roster; adopt it first "
                              f"(dsh_roster action=adopt)")
    if observe:  # bring the mirror up to date; if dsh is down, use what is mirrored
        with contextlib.suppress(dshctl.RpcError):
            state.observe(session_id)
    task = _digest_task(state, session_id, task_id)
    worktree = (task or {}).get("worktree")
    patterns, bad = _check_patterns(state)
    result = digest_mod.build(_mirrored_events(state, session_id), turns=max(int(turns), 1),
                              worktree=worktree, acceptance=(task or {}).get("acceptance"),
                              patterns=patterns)
    if task:
        result = {"task": task["id"], **result}
        dispatches = [d for d in task.get("dispatches", []) if d.get("gitBefore")]
        git = digest_mod.git_changes(worktree, dispatches[-1]["gitBefore"]) if dispatches else None
        if git:
            result["git"] = git
    if bad:
        result["badPatterns"] = bad
    return result


def cmd_digest(state: State, arguments: dict) -> dict:
    if not arguments.get("session"):
        raise dshctl.RpcError("dsh_digest needs a session")
    session_id = dshctl.find_session(arguments["session"], state.client)["sessionId"]
    return digest_for(state, session_id, arguments.get("turns", 1), arguments.get("task_id"))


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
            **({"compactionStalled": auto["compactionStalled"]}
               if auto.get("compactionStalled") else {}),
            **({"retired": True} if notes.get("retired") else {}),
            "notes": notes.get("log", [])[-3:],
        })
    return {"stateDir": str(state.root), "workers": workers}


def cmd_roster(state: State, arguments: dict) -> dict:
    action = arguments.get("action", "show")
    if action == "show":
        if arguments.get("session"):
            session_id = dshctl.find_session(arguments["session"], state.client)["sessionId"]
            worker = state.roster()["workers"].get(session_id)
            if worker is None:
                raise dshctl.RpcError(f"{session_id} is not on the roster")
            return {"worker": worker, "state": state.session_state(session_id)}
        return roster_view(state, arguments.get("include_retired", False))
    if not arguments.get("session"):
        raise dshctl.RpcError(f"action {action!r} needs a session")
    session_id = dshctl.find_session(arguments["session"], state.client)["sessionId"]
    if action == "adopt":
        state.adopt(session_id, how="adopt", role=arguments.get("role"))
        state.observe(session_id)
        return worker_ack(state.roster()["workers"][session_id])
    if action == "note":
        if not arguments.get("text"):
            raise dshctl.RpcError("note needs text")
        return worker_ack(state.add_note(session_id, arguments["text"]))
    if action == "set":
        result = worker_ack(state.set_judgment(session_id, role=arguments.get("role"),
                                               retired=arguments.get("retired")))
        if arguments.get("retired"):
            warning = retire_warning(state, session_id)
            if warning:
                result = {**result, "warning": warning}
        return result
    raise dshctl.RpcError(f"unknown roster action {action!r}")


# dsh compacts a session by itself at about 80% of its window (the default
# thresholdRatio). A worker retired well below that is retired for a reason that
# is not context. The warning is a reminder at the moment of the decision, not a
# block: there are other good reasons to retire a worker.
RETIRE_WARN_BELOW_PCT = 85.0


def retire_warning(state: State, session_id: str) -> str | None:
    with contextlib.suppress(dshctl.RpcError):
        state.observe(session_id)
    pct = (state.session_state(session_id).get("context") or {}).get("usedPct")
    if pct is None or pct >= RETIRE_WARN_BELOW_PCT:
        return None
    return (f"retired at {pct}% context. dsh compacts a session by itself at about "
            "80% and keeps a summary, so context level is not a reason to retire "
            "a worker; a new hire rebuilds what this one knows (PLAYBOOKS section 5, "
            "Context). If context was the reason, undo it with action=set "
            "retired=false. If it was another reason, record it with action=note.")


# ------------------------------------------------------------------- usage
# What a task cost in work, from the numbers dsh reports. Fields dsh does not
# give are left out, never estimated, and no money is computed: prices change
# and belong to the provider.

_TOKEN_FIELDS = {"uncachedInputTokens": "uncachedInput", "cacheReadTokens": "cacheRead",
                 "outputTokens": "output"}
_STAT_FIELDS = {"steps": "steps", "llmMs": "llmMs", "toolMs": "toolMs"}


def usage_snapshot(summary: dict) -> dict:
    """Cumulative totals for a session now, from its projections. Omits what is absent."""
    values = dshctl.projections(summary)
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
    with contextlib.suppress(dshctl.RpcError):
        current = state.client.rpc("session.models", {"sessionId": session_id}).get("current") or {}
        if current.get("model"):
            facts["model"] = (f"{current['provider']}/{current['model']}"
                              if current.get("provider") else current["model"])
        if current.get("reasoningEffort"):
            facts["effort"] = current["reasoningEffort"]
    with contextlib.suppress(dshctl.RpcError):
        summary = dshctl.find_session(session_id, state.client)
        permission = (dshctl.projections(summary).get("permissions") or {}).get("currentValue")
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


def task_ack(task: dict) -> dict:
    """What a write returns: enough to confirm it, not the whole record
    (`dsh_tasks action=show` has the history). The caller just wrote the rest."""
    return {"id": task["id"], "title": task["title"], "status": task["status"],
            "owner": (task.get("owner") or "-").removeprefix("session-")[:8],
            "branch": task.get("branch"), "updated": task["updated"],
            "notes": len(task.get("notes", []))}


def worker_ack(worker: dict) -> dict:
    """The same for roster writes: `dsh_roster action=show session=…` has the rest."""
    auto, notes = worker["auto"], worker["notes"]
    log = notes.get("log", [])
    return {"sessionId": auto.get("sessionId"), "short": auto.get("short"),
            "role": notes.get("role"), "retired": bool(notes.get("retired")),
            "notes": len(log), **({"lastNote": log[-1]["text"][:200]} if log else {})}


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

