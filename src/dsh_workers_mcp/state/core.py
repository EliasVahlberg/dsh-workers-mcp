"""
`State`: one orchestrator state directory, with the rules for the roster, the
history mirror and the tasks.

The class is the join of three mixins on one `Store`. Callers use `State` and
nothing else.
"""

from __future__ import annotations

from dsh_workers_mcp import sessions
from dsh_workers_mcp.state.history import (
    HistoryMixin,
    _observed_state,
    _update_blocked,
    _worker_auto,
)
from dsh_workers_mcp.state.roster import RosterMixin
from dsh_workers_mcp.state.store import _write_json
from dsh_workers_mcp.state.tasks import TasksMixin
from dsh_workers_mcp.state.usage import usage_snapshot


class State(RosterMixin, HistoryMixin, TasksMixin):
    """One orchestrator state directory. Created lazily on first write."""

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
        summary = summary or sessions.find_session(session_id, self.client)
        prev = self.session_state(session_id)
        state = _observed_state(prev, summary, synced["new"], synced["lastSeq"], session_id)
        state["compaction"] = self._track_compaction(session_id, prev, state, synced["new"])
        state["usage"] = usage_snapshot(summary)
        _update_blocked(state, prev, blocked, unblocked)
        _write_json(self.session_dir(session_id) / "state.json", state)
        self._update_worker(session_id, auto=_worker_auto(state, summary))
        self._advance_task(session_id, state, blocked=bool(blocked), unblocked=unblocked)
        return state
