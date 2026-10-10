"""
Reading sessions: find one, read its projections, page its events, take its
latest reply.

Event-stream gotcha that drives the whole design: session.history's
`maxMessages` counts *messages*, and each assistant message expands into
hundreds of `assistant/chunk` streaming deltas (measured: 3 messages ->
1437 events, 1424 of them chunks). Everything here filters chunks by default;
never hand raw history to a model's context.
"""

from __future__ import annotations

import json

from dsh_workers_mcp.transport import DshClient, RpcError

HISTORY_PAGE = 200

# Streaming deltas: redundant with the committed assistant/message that follows.
NOISE_TYPES = {"assistant/chunk", "reasoning/chunk"}

# Content blocks carrying the model's private reasoning rather than its answer.
# These are separate blocks that ALSO have a `text` field, so a naive "any block
# with text" extractor silently concatenates thinking into the visible reply.
REASONING_BLOCKS = {"reasoning", "thinking"}



# ---------------------------------------------------------------- session view

def find_session(session_ref: str, client: DshClient) -> dict:
    """Resolve a full or partial session id to its session.list summary."""
    items = client.rpc("session.list").get("items", [])
    exact = [s for s in items if s["sessionId"] == session_ref]
    if exact:
        return exact[0]
    partial = [s for s in items if session_ref in s["sessionId"]]
    if len(partial) == 1:
        return partial[0]
    if not partial:
        raise RpcError(f"no session matching {session_ref!r} (have {len(items)})")
    raise RpcError(f"{session_ref!r} is ambiguous: {[s['sessionId'] for s in partial]}")


def projections(summary: dict) -> dict:
    return (summary.get("projections") or {}).get("values") or {}


def text_of(event: dict, include_reasoning: bool = False) -> str:
    """
    Best-effort plain text for one session event, '' when it carries none.

    Reasoning blocks are excluded by default: they are distinct content blocks
    that nonetheless carry a `text` field, so including them mixes the model's
    private thinking into what reads as its answer.
    """
    data = event.get("data") or {}
    etype = event.get("type", "")

    if etype in ("assistant/message", "user/message", "tool/result"):
        # assistant/tool events nest content under `message`; user/message
        # carries it directly on `data` (verified on live events). Reading only
        # `message` made every real prompt read as empty.
        message = data.get("message") or data
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if not include_reasoning and block.get("type") in REASONING_BLOCKS:
                    continue
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
            return "\n".join(parts)
        return ""
    if etype == "tool/call":
        return f"{data.get('name', '?')} {json.dumps(data.get('arguments'))[:600]}"
    return ""


def reasoning_of(event: dict) -> str:
    """Just the reasoning blocks of one event, '' when there are none."""
    content = ((event.get("data") or {}).get("message") or {}).get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content
        if isinstance(block, dict) and block.get("type") in REASONING_BLOCKS
        and isinstance(block.get("text"), str)
    )


def fetch_events(session_id: str, client: DshClient, max_messages: int = 40,
                 keep_noise: bool = False) -> list[dict]:
    """One page of history, newest-anchored, with streaming chunks dropped."""
    value = client.rpc("session.history",
                {"sessionId": session_id, "maxMessages": max_messages})
    events = [e.get("event", e) for e in value.get("events", [])]
    if keep_noise:
        return events
    return [e for e in events if e.get("type") not in NOISE_TYPES]


def walk_events(session_id: str, client: DshClient, max_messages: int,
                pages: int) -> tuple[list[dict], bool]:
    """
    Page backwards through history, newest first, dropping streaming chunks.

    Returns (events_oldest_first, exhausted). `pages` bounds the walk because a
    long session's full history is very large: one page of 200 messages is
    already ~1000 non-chunk events.
    """
    collected: list[dict] = []
    before: int | None = None
    exhausted = False
    for _ in range(max(1, pages)):
        payload: dict = {"sessionId": session_id, "maxMessages": max_messages}
        if before is not None:
            payload["beforeSeq"] = before
        value = client.rpc("session.history", payload)
        raw = [e.get("event", e) for e in value.get("events", [])]
        if not raw:
            exhausted = True
            break
        collected = [e for e in raw if e.get("type") not in NOISE_TYPES] + collected
        if not value.get("hasMore"):
            exhausted = True
            break
        before = min((e.get("seq", 0) for e in raw), default=0)
        if before <= 0:
            exhausted = True
            break
    return collected, exhausted


def latest_reply(session_id: str, client: DshClient, after_seq: int = -1,
                 max_messages: int = 8) -> tuple[int, str, str]:
    """(seq, answer_text, reasoning_text) of the newest assistant message."""
    for event in reversed(fetch_events(session_id, client, max_messages)):
        if event.get("type") == "assistant/message" and event.get("seq", -1) > after_seq:
            return event.get("seq", -1), text_of(event), reasoning_of(event)
    return -1, "", ""


def last_turn_end(session_id: str, client: DshClient, max_messages: int = 8) -> dict | None:
    """
    Newest turn/end reason, or None when none is in the recent window.

    Load-bearing for unattended work: a turn that ended in `error`,
    `max-tokens`, or `aborted` leaves the session `idle` exactly like a clean
    `completed` one, so polling `state` alone cannot tell a finished worker from
    a broken one. The full set (TurnEndReasonMap in
    packages/core/session/src/types.ts) is completed | aborted | blocked |
    error | max-tokens | interrupted; `aborted` carries a cause and `error`
    carries a structured LlmFailure, both preserved here.
    """
    for event in reversed(fetch_events(session_id, client, max_messages)):
        if event.get("type") == "turn/end":
            reason = (event.get("data") or {}).get("reason") or {}
            extra = {k: v for k, v in reason.items() if k != "kind"}
            return {"seq": event.get("seq"), "kind": reason.get("kind"), **extra}
    return None


def running_now(session_id: str, client: DshClient) -> bool:
    return bool(find_session(session_id, client).get("running"))


