"""
The roster: the workers that this server manages, the facts it observed about
them (`auto`) and the judgment of the lead (`notes`), and the `dsh_roster` tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import contextlib

from dsh_workers_mcp import dshctl
from dsh_workers_mcp.state.store import Store, _write_json, now_iso

if TYPE_CHECKING:
    from dsh_workers_mcp.state.core import State

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



def worker_ack(worker: dict) -> dict:
    """The same for roster writes: `dsh_roster action=show session=…` has the rest."""
    auto, notes = worker["auto"], worker["notes"]
    log = notes.get("log", [])
    return {"sessionId": auto.get("sessionId"), "short": auto.get("short"),
            "role": notes.get("role"), "retired": bool(notes.get("retired")),
            "notes": len(log), **({"lastNote": log[-1]["text"][:200]} if log else {})}



class RosterMixin(Store):
    """The roster: adopt, note, judge and reconcile workers."""

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
