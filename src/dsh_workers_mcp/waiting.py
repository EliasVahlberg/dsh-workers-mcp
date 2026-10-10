"""Sending work and waiting for it: prompts, turn ends, and waiting on one or several sessions."""

from __future__ import annotations

import contextlib
import time

from dsh_workers_mcp import pending, sessions
from dsh_workers_mcp.transport import DshClient, RpcError


def blocked_among(client: DshClient, session_ids: set[str]) -> tuple[list[dict], bool]:
    """
    The questions and approvals that wait on these sessions, and whether the check worked.

    A wait polls for a long time. One failed poll, such as a slow handshake while
    many workers run test suites, must not end it. The check returns ([], False)
    then, and the wait tries again at the next poll.
    """
    try:
        items = pending.pending_requests(client, 0.8, 4.0)
    except RpcError:
        return [], False
    return [item for item in items if item.get("sessionId") in session_ids], True


def _degraded(failed: int) -> dict:
    """A note for the result of a wait that could not check for pending requests at some polls."""
    return {"pendingChecksFailed": failed} if failed else {}


def send_prompt(session_id: str, text: str, client: DshClient, mode: str) -> None:
    client.rpc("session.prompt", {
        "sessionId": session_id,
        "mode": mode,
        "content": [{"type": "text", "text": text}],
    })


def head_seq(session_id: str, client: DshClient) -> int:
    value = client.rpc("session.history", {"sessionId": session_id, "maxMessages": 1})
    return (value.get("sessions.projections") or {}).get("asOfSeq", -1)


def wait_for_turn_end(session_id: str, client: DshClient, after_seq: int,
                      timeout: float, poll: float,
                      watch_pending: bool = False) -> tuple[bool, dict]:
    """
    Block until a turn/end lands after after_seq. Returns (completed, info).

    With watch_pending, also returns early -- completed=False, info["blocked"]
    -- when the worker has an outstanding question or approval. Without that, a
    worker waiting on an answer is indistinguishable from one still working: it
    reports `running` and emits nothing, so a waiter sits out its whole timeout
    (measured: 40 s timeout, approval pending the entire time, `running`
    throughout). Blocking on an answer only the waiter can give is a deadlock.
    """
    deadline = time.time() + timeout
    saw_running = False
    failed = 0
    while time.time() < deadline:
        time.sleep(poll)
        try:
            summary = sessions.find_session(session_id, client)
        except RpcError:
            continue
        running = bool(summary.get("running"))
        saw_running = saw_running or running
        for event in reversed(sessions.fetch_events(session_id, client, 4)):
            if event.get("type") == "turn/end" and event.get("seq", -1) > after_seq:
                return True, {"reason": (event.get("data") or {}).get("reason"),
                              "seq": event.get("seq")}
        if watch_pending and running:
            blocked, checked = blocked_among(client, {session_id})
            failed += not checked
            if blocked:
                return False, {"blocked": True, "pending": blocked,
                               "hint": "answer with dsh_answer (questions) or "
                                       "dsh_approve (approvals), then wait again"}
        # Idle again without ever going busy and no new turn: nothing was queued.
        if saw_running and not running:
            continue
    return False, {"timedOut": True, "waited": timeout, **_degraded(failed)}


def wait_for_any(refs: list[str], client: DshClient, timeout: float, poll: float) -> dict:
    """
    Block until the FIRST of several workers ends a turn or blocks on you.

    A lead with two workers running cannot know which finishes first; waiting
    on the one it guesses leaves the other idle for up to the whole timeout
    (measured: three 15-minute waits in one afternoon, each on the slower
    worker). An idle worker among them returns at once, because an idle worker
    is exactly what the lead needs to hear about.
    """
    afters: dict[str, int] = {}
    for ref in refs:
        summary = sessions.find_session(ref, client)
        session_id = summary["sessionId"]
        if not summary.get("running"):
            last = sessions.last_turn_end(session_id, client)
            return {"sessionId": session_id, "completed": last is not None,
                    "alreadyIdle": True, **(last or {})}
        afters[session_id] = head_seq(session_id, client)

    deadline = time.time() + timeout
    failed = 0
    while time.time() < deadline:
        time.sleep(poll)
        for session_id, after in afters.items():
            with contextlib.suppress(RpcError):
                for event in reversed(sessions.fetch_events(session_id, client, 4)):
                    if event.get("type") == "turn/end" and event.get("seq", -1) > after:
                        reason = (event.get("data") or {}).get("reason") or {}
                        return {"sessionId": session_id, "completed": True,
                                "kind": reason.get("kind"), "reason": reason,
                                "seq": event.get("seq"),
                                "stillRunning": [s for s in afters if s != session_id]}
        blocked, checked = blocked_among(client, set(afters))
        failed += not checked
        if blocked:
            return {"sessionId": blocked[0]["sessionId"], "completed": False,
                    "blocked": True, "pending": blocked,
                    "hint": "answer with dsh_answer (questions) or dsh_approve "
                            "(approvals), then wait again"}
    return {"completed": False, "timedOut": True, "waited": timeout,
            "stillRunning": list(afters), **_degraded(failed)}


def turn_end_seq(session_id: str, turn: int, client: DshClient) -> int:
    """The seq of turn `turn`'s turn/end, paging back through the history."""
    before = None
    while True:
        value = client.rpc("session.history", {"sessionId": session_id, "maxMessages": sessions.HISTORY_PAGE,
                                        **({"beforeSeq": before} if before else {})})
        events = [item.get("event") or item for item in value.get("events", [])]
        for event in events:
            if event.get("type") == "turn/end" and (event.get("data") or {}).get("turn") == turn:
                return event["seq"]
        if not value.get("hasMore") or not events:
            raise RpcError(f"turn {turn} has no turn/end in this session's history")
        before = min(event["seq"] for event in events)


