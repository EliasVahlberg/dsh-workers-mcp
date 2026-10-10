"""
What a session did, as the server saw it: the mirror of its history, the
`state.json` snapshot, and the compaction watch.

The functions here need no state directory. `HistoryMixin` adds the one method
that does I/O, `sync_history`, to `State`.
"""

from __future__ import annotations

import json

from dsh_workers_mcp import dshctl
from dsh_workers_mcp.state.store import Store, _read_json, _write_json, ms_to_iso, now_iso

PREVIEW_CHARS = 2_000       # prompt/reply text kept in state.json
HISTORY_PAGE_MESSAGES = 200
HISTORY_MAX_PAGES = 1_000   # first mirror of a long session walks it all once


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


def _user_prompt(event: dict) -> bool:
    """A real prompt, not a plugin-injected message (runtime context, approval notices)."""
    source = (event.get("data") or {}).get("source") or {}
    return event.get("type") == "user/message" and source.get("kind") == "user"

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



class HistoryMixin(Store):
    """Mirror a session's history, and watch its compaction."""

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

