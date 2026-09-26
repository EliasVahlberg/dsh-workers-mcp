#!/usr/bin/env python3
"""
dshctl — orchestrator-side control of dsh worker sessions over the live
`dsh web` server's JSON-RPC-ish HTTP API.

Transport notes (reverse-engineered from packages/host/apiproxy + verified live):
  * endpoint is POST /api/<method>, NOT a single RPC endpoint with a method field
  * envelope is {"type":"client-request","rpcId":<str>,"method":<str>,"payload":{...}}
    -- this is NOT JSON-RPC 2.0 (no "jsonrpc" field, "rpcId" not "id")
  * response is {"type":"server-response","rpcId":...,"result":{"ok":bool,...}}
  * loopback requests are trusted with no credentials

Event-stream gotcha that drives the whole design: session.history's
`maxMessages` counts *messages*, and each assistant message expands into
hundreds of `assistant/chunk` streaming deltas (measured: 3 messages ->
1437 events, 1424 of them chunks). Everything here filters chunks by default;
never hand raw history to a model's context.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE = "http://127.0.0.1:3080"

# Streaming deltas: redundant with the committed assistant/message that follows.
NOISE_TYPES = {"assistant/chunk", "reasoning/chunk"}

# Content blocks carrying the model's private reasoning rather than its answer.
# These are separate blocks that ALSO have a `text` field, so a naive "any block
# with text" extractor silently concatenates thinking into the visible reply.
REASONING_BLOCKS = {"reasoning", "thinking"}


class RpcError(RuntimeError):
    pass


def rpc(method: str, payload: dict | None = None, base: str = DEFAULT_BASE,
        timeout: float = 120.0) -> dict:
    """One RPC round trip. Raises RpcError on transport or ok:false failure."""
    body = json.dumps({
        "type": "client-request",
        "rpcId": f"dshctl-{int(time.time() * 1000)}",
        "method": method,
        "payload": payload or {},
    }).encode()
    req = urllib.request.Request(
        f"{base}/api/{method}", data=body,
        headers={"content-type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RpcError(f"{method}: HTTP {exc.code}: {exc.read()[:400].decode(errors='replace')}") from exc
    except urllib.error.URLError as exc:
        raise RpcError(f"{method}: cannot reach {base} ({exc.reason}) — is `dsh web` running?") from exc
    result = parsed.get("result") or {}
    if not result.get("ok"):
        raise RpcError(f"{method}: {json.dumps(result.get('error'), indent=2)}")
    return result.get("value") or {}


# ---------------------------------------------------------------- session view

def find_session(session_ref: str, base: str) -> dict:
    """Resolve a full or partial session id to its session.list summary."""
    items = rpc("session.list", base=base).get("items", [])
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
        message = data.get("message") or {}
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


def fetch_events(session_id: str, base: str, max_messages: int = 40,
                 keep_noise: bool = False) -> list[dict]:
    """One page of history, newest-anchored, with streaming chunks dropped."""
    value = rpc("session.history",
                {"sessionId": session_id, "maxMessages": max_messages}, base=base)
    events = [e.get("event", e) for e in value.get("events", [])]
    if keep_noise:
        return events
    return [e for e in events if e.get("type") not in NOISE_TYPES]


def walk_events(session_id: str, base: str, max_messages: int,
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
        value = rpc("session.history", payload, base=base)
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


def latest_reply(session_id: str, base: str, after_seq: int = -1,
                 max_messages: int = 8) -> tuple[int, str, str]:
    """(seq, answer_text, reasoning_text) of the newest assistant message."""
    for event in reversed(fetch_events(session_id, base, max_messages)):
        if event.get("type") == "assistant/message" and event.get("seq", -1) > after_seq:
            return event.get("seq", -1), text_of(event), reasoning_of(event)
    return -1, "", ""


def last_turn_end(session_id: str, base: str, max_messages: int = 8) -> dict | None:
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
    for event in reversed(fetch_events(session_id, base, max_messages)):
        if event.get("type") == "turn/end":
            reason = (event.get("data") or {}).get("reason") or {}
            extra = {k: v for k, v in reason.items() if k != "kind"}
            return {"seq": event.get("seq"), "kind": reason.get("kind"), **extra}
    return None


# ------------------------------------------------------------------- commands

def cmd_list(args) -> int:
    items = rpc("session.list", base=args.base).get("items", [])
    items.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
    rows = []
    for summary in items:
        if summary.get("blank") and not args.all:
            continue
        values = projections(summary)
        pressure = values.get("contextPressure") or {}
        window = pressure.get("contextWindow") or 0
        used = pressure.get("pressureTokens") or 0
        rows.append({
            "sessionId": summary["sessionId"],
            "running": bool(summary.get("running")),
            "preset": summary.get("agentPreset"),
            "cwd": summary.get("cwd"),
            "title": (values.get("title") or "")[:60],
            "pressurePct": round(used / window * 100, 1) if window else None,
        })
    print(json.dumps(rows[: args.limit], indent=2))
    return 0


def cmd_status(args) -> int:
    summary = find_session(args.session, args.base)
    values = projections(summary)
    stats = values.get("sessionStats") or {}
    usage = values.get("tokenUsage") or {}
    pressure = values.get("contextPressure") or {}
    window = pressure.get("contextWindow") or 0
    used = pressure.get("pressureTokens") or 0
    cache_read = usage.get("cacheReadTokens") or 0
    uncached = usage.get("uncachedInputTokens") or 0
    seq, reply, reasoning = latest_reply(summary["sessionId"], args.base)

    out = {
        "sessionId": summary["sessionId"],
        "state": "running" if summary.get("running") else "idle",
        "preset": summary.get("agentPreset"),
        "cwd": summary.get("cwd"),
        "title": values.get("title"),
        "goal": values.get("goal"),
        "context": {
            "window": window,
            "used": used,
            "usedPct": round(used / window * 100, 1) if window else None,
            "breakdown": values.get("contextBreakdown"),
        },
        "work": {
            "turns": stats.get("turns"),
            "steps": stats.get("steps"),
            "llmMinutes": round((stats.get("llmMs") or 0) / 60000, 1),
            "toolMinutes": round((stats.get("toolMs") or 0) / 60000, 1),
        },
        "tokens": {
            "uncachedInput": uncached,
            "cacheRead": cache_read,
            "output": usage.get("outputTokens"),
            "cacheHitPct": round(cache_read / (cache_read + uncached) * 100, 2)
            if (cache_read + uncached) else None,
        },
        "latestReply": {"seq": seq, "text": reply[: args.chars],
                        "reasoningChars": len(reasoning),
                        **({"reasoning": reasoning[: args.chars]}
                           if getattr(args, "reasoning", False) else {})},
        "lastTurnEnd": last_turn_end(summary["sessionId"], args.base),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_grep(args) -> int:
    summary = find_session(args.session, args.base)
    pattern = re.compile(args.pattern, 0 if args.case_sensitive else re.IGNORECASE)
    events, exhausted = walk_events(summary["sessionId"], args.base,
                                    args.max_messages, args.pages)
    hits = []
    for event in events:
        body = text_of(event)
        if not body:
            continue
        for match in pattern.finditer(body):
            start = max(0, match.start() - args.context)
            hits.append({
                "seq": event.get("seq"),
                "type": event.get("type"),
                "snippet": body[start: match.end() + args.context].replace("\n", " "),
            })
            break
    span = [events[0].get("seq"), events[-1].get("seq")] if events else None
    print(json.dumps({
        "sessionId": summary["sessionId"],
        "scanned": len(events),
        "seqSpan": span,
        "reachedSessionStart": exhausted,
        "hits": len(hits),
        "results": hits[: args.limit],
    }, indent=2))
    return 0


def cmd_search(args) -> int:
    """Server-side cross-session search (title/content snippets, capped)."""
    value = rpc("session.search", {"query": args.query}, base=args.base)
    print(json.dumps(value, indent=2)[: args.chars])
    return 0


# ------------------------------------------- pending server-requests (WS)
#
# A worker that asks the user something does NOT end its turn in a distinct
# state -- TurnEndReasonMap has no input-required variant, so a blocked worker
# and a finished one both read `completed` + `idle`. The real signal lives on a
# different plane: an answerable *server-request* frame on the mux downlink.
#
# The lever that makes this usable from a discrete-call tool like this one: the
# stream replays every still-pending approval/question frame on open, reusing
# each rpcId verbatim -- "the refresh-recovery baseline"
# (packages/host/apiproxy/src/api/events.ts). So a short-lived connection is a
# complete snapshot of outstanding work; we never hold a socket open.
#
# Transport: the running `dsh web` server serves these two paths as WebSocket
# UPGRADES, not SSE -- a plain GET answers `426 upgrade required`
# (packages/client/connection/src/index.ts). The fetch/SSE carrier in
# host/apiproxy is a different deployment of the same API; do not assume it.
# Each text message is JSON.stringify(ServerRequest), so the rpcId to echo is on
# that envelope, not inside the payload
# (packages/client/connection/src/websocket-downlink.ts).
#
# We only ever read. Per that same file, "client messages are a protocol
# violation: upstream traffic remains on HTTP" -- answers go back over
# POST /api/respond. That makes a minimal read-only client sufficient, so this
# stays dependency-free: server-to-client frames are never masked.

ANSWERABLE = {"question/requested", "approval/requested"}

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_connect(path: str, base: str, timeout: float):
    """Handshake a WebSocket and return (socket, buffered_reader)."""
    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        raise RpcError("wss:// is not supported; dsh web serves plain loopback")
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise RpcError(
            f"{path}: cannot reach {base} ({exc}) — is `dsh web` running?") from exc
    sock.settimeout(timeout)
    sock.sendall(request.encode())
    reader = sock.makefile("rb")
    status = reader.readline().decode("latin-1").strip()
    if "101" not in status:
        sock.close()
        raise RpcError(f"{path}: WebSocket upgrade refused ({status!r})")
    while True:  # drain the response headers
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    expected = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
    return sock, reader, expected


def _ws_frames(reader, deadline: float):
    """Yield text payloads off an unmasked server-to-client WebSocket stream."""
    pending = ""
    while time.time() < deadline:
        header = reader.read(2)
        if not header or len(header) < 2:
            return
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(reader.read(2), "big")
        elif length == 127:
            length = int.from_bytes(reader.read(8), "big")
        payload = reader.read(length) if length else b""
        if opcode == 0x8:  # close
            return
        if opcode in (0x9, 0xA):  # ping/pong: nothing to do on a short read
            continue
        if opcode in (0x0, 0x1):  # continuation or text
            pending += payload.decode("utf-8", "replace")
            if fin:
                yield pending
                pending = ""


def read_stream(path: str, base: str, settle: float = 1.5,
                max_seconds: float = 8.0) -> list[dict]:
    """
    Open a downlink, collect frames until it goes quiet, then close.

    Connecting and reading fail differently on purpose. A refused connection or
    a rejected upgrade raises, because "no frames" and "no server" must not look
    alike -- an empty result would otherwise read as "nothing is pending" when
    the truth is that nothing was asked. Once the stream is open, a read that
    goes quiet is the normal stop condition: the replayed baseline has been
    delivered in full, so whatever was collected is returned.
    """
    frames: list[dict] = []
    deadline = time.time() + max_seconds
    sock, reader, _ = _ws_connect(path, base, settle)  # raises on failure
    try:
        for text in _ws_frames(reader, deadline):
            try:
                frames.append(json.loads(text))
            except json.JSONDecodeError:
                continue  # one corrupt frame must not kill the read
    except (TimeoutError, OSError):
        pass  # stream went quiet: the baseline is in
    finally:
        reader.close()
        sock.close()
    return frames


def pending_requests(base: str, settle: float, max_seconds: float) -> list[dict]:
    """Every outstanding question and approval, keyed by the rpcId to echo."""
    out: dict[str, dict] = {}
    for frame in read_stream("/api/events.mux", base, settle, max_seconds):
        payload = frame.get("payload") or {}
        kind = payload.get("type")
        if kind not in ANSWERABLE:
            continue
        detail = ({"questions": payload.get("questions")}
                  if kind == "question/requested"
                  else {"approvalId": payload.get("approvalId"),
                        "toolName": payload.get("toolName"),
                        "callId": payload.get("callId"),
                        "reason": payload.get("reason")})
        out[str(frame.get("rpcId"))] = {
            "rpcId": frame.get("rpcId"),
            "kind": kind,
            "sessionId": payload.get("sessionId"),
            **detail,
        }
    return list(out.values())


TOOL_POLICY_MARK = "[tool-policy:"


def tool_call_arguments(session_id: str, call_id: str, base: str) -> object:
    """
    The arguments of one tool call, found by callId in the session log.

    An approval request deliberately omits arguments (dsh attaches the prompt to
    the already-streamed call instead of duplicating it), so without this join
    an approver sees only a tool name and a reason -- and the reason is usually
    model-written. The arguments are what is actually about to run.
    """
    for event in reversed(fetch_events(session_id, base, 12)):
        data = event.get("data") or {}
        if event.get("type") == "tool/call" and data.get("callId") == call_id:
            raw = data.get("arguments")
            try:
                return json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                return raw
    return None


def describe_approval(item: dict, base: str) -> dict:
    """Add the tool arguments and say who wrote the reason."""
    reason = item.get("reason") or ""
    out = dict(item)
    if item.get("callId"):
        out["arguments"] = tool_call_arguments(item["sessionId"], item["callId"], base)
    # tool-policy reasons come from our own config; everything else here --
    # notably a sandbox escalation's justification -- is text the worker's model
    # wrote, and a hijacked worker controls it.
    out["reasonAuthor"] = ("tool-policy rule (trusted config)"
                           if TOOL_POLICY_MARK in reason
                           else "worker model (untrusted -- judge the arguments, not the prose)")
    return out


def cmd_pending(args) -> int:
    items = pending_requests(args.base, args.settle, args.timeout)
    if args.session:
        wanted = find_session(args.session, args.base)["sessionId"]
        items = [i for i in items if i.get("sessionId") == wanted]
    items = [describe_approval(i, args.base) if i["kind"] == "approval/requested" else i
             for i in items]
    print(json.dumps(items, indent=2))
    return 0


def cmd_approve(args) -> int:
    """
    Grant or reject a pending tool approval.

    `allowed-once` grants exactly the asked-about call and nothing wider;
    `rejected` makes the tool return an error the model sees as a refusal.
    Those are the only two outcomes the wire accepts.
    """
    pending = [i for i in pending_requests(args.base, 1.5, 8.0) if i["rpcId"] == args.rpc_id]
    if not pending:
        raise RpcError(f"rpcId {args.rpc_id!r} is not pending — already settled, "
                       f"or the turn was interrupted. Re-read `pending`.")
    item = pending[0]
    if item["kind"] != "approval/requested":
        raise RpcError(f"rpcId {args.rpc_id!r} is a {item['kind']}; answer questions "
                       f"with `answer`, not `approve`.")
    wanted = find_session(args.session, args.base)["sessionId"]
    if item["sessionId"] != wanted:
        raise RpcError(f"rpcId {args.rpc_id!r} belongs to {item['sessionId']}, not {wanted}")

    body = json.dumps({
        "type": "client-response",
        "rpcId": args.rpc_id,
        "result": {"ok": True, "value": {
            "sessionId": item["sessionId"],
            "approvalId": item["approvalId"],
            "outcome": args.outcome,
        }},
    }).encode()
    req = urllib.request.Request(f"{args.base}/api/respond", data=body,
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            receipt = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RpcError(f"respond: HTTP {exc.code}: "
                       f"{exc.read()[:400].decode(errors='replace')}") from exc
    print(json.dumps({"receipt": receipt, "outcome": args.outcome,
                      "toolName": item.get("toolName"),
                      "arguments": describe_approval(item, args.base).get("arguments")},
                     indent=2))
    return 0 if receipt.get("accepted") else 2


def execute_command(session_id: str, line: str, base: str) -> dict:
    """
    Run a host slash command against one session, without sending it to the model.

    This is the second RPC family (typert remotes): POST /api/<namespace>/<method>
    with the payload wrapped in exactly one `args` object. Sending the same line
    through `session.prompt` does NOT route it as a command -- the model receives
    it as ordinary text and starts a turn. Verified the hard way.
    """
    body = json.dumps({"type": "client-request", "rpcId": f"cmd-{uuid.uuid4()}",
                       "method": "commands/execute",
                       "payload": {"args": {"agentId": session_id, "line": line,
                                            "images": []}}}).encode()
    req = urllib.request.Request(f"{base}/api/commands/execute", data=body,
                                 headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            envelope = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RpcError(f"commands/execute: HTTP {exc.code}: "
                       f"{exc.read()[:400].decode(errors='replace')}") from exc
    result = envelope.get("result") or {}
    if not result.get("ok"):
        raise RpcError(f"commands/execute: {json.dumps(result.get('error'))}")
    value = result.get("value")
    if value is None:
        raise RpcError(f"{line.split()[0]!r} is not a registered command in this deployment")
    return value["result"]


_PRESET_LINE = re.compile(r"current preset (\S+) \(available: ([^)]*)\)")


def get_permission(session_id: str, base: str) -> dict:
    text = execute_command(session_id, "/permission", base).get("text", "")
    match = _PRESET_LINE.search(text)
    if not match:
        raise RpcError(f"unexpected /permission reply: {text!r}")
    return {"preset": match.group(1),
            "available": [n.strip() for n in match.group(2).split(",") if n.strip()]}


def set_permission(session_id: str, preset: str, base: str) -> dict:
    before = get_permission(session_id, base)
    if preset not in before["available"]:
        raise RpcError(f"unknown preset {preset!r}; available: {before['available']}")
    result = execute_command(session_id, f"/permission {preset}", base)
    if result.get("kind") == "error":
        raise RpcError(f"/permission {preset}: {result.get('text')}")
    after = get_permission(session_id, base)
    # Read back rather than trusting the command's success text: the preset a
    # session *reports* is derived from its knobs, so a mismatch here would mean
    # the switch did not take.
    if after["preset"] != preset:
        raise RpcError(f"switched to {preset!r} but session now reports {after['preset']!r}")
    return {"previous": before["preset"], **after}


def cmd_permission(args) -> int:
    session_id = find_session(args.session, args.base)["sessionId"]
    if args.preset:
        if running_now(session_id, args.base) and not args.force:
            raise RpcError("worker is mid-turn; the new preset would apply from its "
                           "next tool call. Pass --force to switch anyway.")
        out = set_permission(session_id, args.preset, args.base)
    else:
        out = get_permission(session_id, args.base)
    print(json.dumps({"sessionId": session_id, **out}, indent=2))
    return 0


def running_now(session_id: str, base: str) -> bool:
    return bool(find_session(session_id, base).get("running"))


def build_answers(args) -> list[dict]:
    """
    Assemble the answer batch. One ask() carries many questions and takes ONE
    batch answer covering them -- never a response per question -- so both input
    forms build a list, not a single item.
    """
    if args.answers:
        parsed = json.loads(args.answers)
        if not isinstance(parsed, list):
            raise RpcError("--answers must be a JSON array of {id,selected,custom?}")
        return parsed
    answers: list[dict] = []
    for pair in args.answer or []:
        if "=" not in pair:
            raise RpcError(f"--answer must look like questionId=Label (got {pair!r})")
        qid, label = pair.split("=", 1)
        answers.append({"id": qid, "selected": [label]})
    if args.custom:
        if not answers:
            raise RpcError("--custom needs a question id; use --answer id= --custom TEXT")
        answers[-1]["custom"] = args.custom
    if not answers:
        raise RpcError("answer needs --answer or --answers")
    return answers


def validate_answers(answers: list[dict], questions: list[dict]) -> None:
    """
    Check a batch against the exact rules the host applies, before sending.

    Mirrors `matchesQuestions` in packages/host/apiproxy/src/api-proxy.ts. Worth
    duplicating because the wire gives back only `{accepted:false, reason:
    'bad-response'}` with no indication of which rule failed -- and the rules are
    not guessable. Getting this wrong leaves the worker still blocked while the
    call looks like it did something.
    """
    if len(answers) != len(questions):
        raise RpcError(
            f"answer the whole batch: {len(questions)} question(s) asked, "
            f"{len(answers)} answer(s) given")

    for index, (answer, question) in enumerate(zip(answers, questions)):
        where = f"answer {index + 1} (id={answer.get('id')!r})"
        if answer.get("id") != question.get("id"):
            raise RpcError(
                f"{where}: must match question {index + 1} by position — "
                f"expected id {question.get('id')!r}. Answers are matched by "
                f"order, not looked up by id.")

        selected = answer.get("selected") or []
        if len(set(selected)) != len(selected):
            raise RpcError(f"{where}: duplicate labels in `selected`")

        custom = (answer.get("custom") or "").strip()
        if "custom" in answer and not custom:
            raise RpcError(f"{where}: `custom` must not be blank; omit it instead")

        if question.get("multiSelect") is not True:
            if custom and selected:
                raise RpcError(
                    f"{where}: this question is single-select, so `custom` and "
                    f"`selected` are mutually exclusive — send one or the other")
            if len(selected) > 1:
                raise RpcError(
                    f"{where}: single-select question takes at most one label, "
                    f"got {len(selected)}")

        labels = {option.get("label") for option in question.get("options") or []}
        unknown = [label for label in selected if label not in labels]
        if unknown:
            raise RpcError(
                f"{where}: {unknown!r} are not offered options. Labels must be "
                f"verbatim; valid: {sorted(labels)!r}")


def cmd_answer(args) -> int:
    """Settle a pending question by echoing its rpcId back to /api/respond."""
    session_id = find_session(args.session, args.base)["sessionId"]
    answers = build_answers(args)

    # Validate against the live request rather than trusting the caller: the
    # rules depend on the question (multiSelect, the exact option labels), so
    # they cannot be checked from the answer alone.
    pending = [item for item in pending_requests(args.base, 1.5, 8.0)
               if item["rpcId"] == args.rpc_id]
    if not pending:
        raise RpcError(
            f"rpcId {args.rpc_id!r} is not pending — it may have been answered "
            f"already, or the turn was interrupted. Re-read `pending`.")
    if pending[0]["kind"] != "question/requested":
        raise RpcError(
            f"rpcId {args.rpc_id!r} is a {pending[0]['kind']}, not a question. "
            f"This tool cannot grant tool approvals.")
    validate_answers(answers, pending[0].get("questions") or [])

    body = json.dumps({
        "type": "client-response",
        "rpcId": args.rpc_id,
        "result": {"ok": True, "value": {
            "sessionId": session_id,
            "answer": {"answers": answers},
        }},
    }).encode()
    req = urllib.request.Request(
        f"{args.base}/api/respond", data=body,
        headers={"content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            receipt = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise RpcError(f"respond: HTTP {exc.code}: "
                       f"{exc.read()[:400].decode(errors='replace')}") from exc
    print(json.dumps(receipt, indent=2))
    # A late or duplicate answer comes back accepted:false reason:'not-pending'.
    return 0 if receipt.get("accepted") else 2


# ------------------------------------------------------------------- goals
#
# The goal is the one piece of orientation that survives compaction as
# structured state rather than as prose in a summary, which makes it the
# durable home for "what this worker is for". Every goal.* method is a
# mutation; current state is read only from the 'goal' session projection
# (packages/host/apiproxy/src/api/goals.schema.ts). Mutations carry a
# {id, revision} ref, so a stale revision is rejected rather than silently
# clobbering a concurrent edit.

def current_goal(session_ref: str, base: str) -> dict:
    goal = (projections(find_session(session_ref, base)).get("goal") or {}).get("goal") or {}
    if not goal.get("id"):
        raise RpcError("session has no goal — create one with `goal set`")
    return goal


def cmd_goal(args) -> int:
    session_id = find_session(args.session, args.base)["sessionId"]

    if args.action == "show":
        print(json.dumps(projections(find_session(session_id, args.base)).get("goal"),
                         indent=2))
        return 0

    if args.action == "set":
        if not args.objective:
            raise RpcError("goal set requires --objective")
        payload: dict = {"sessionId": session_id, "objective": args.objective}
        if args.max_rounds is not None:
            payload["maxGoalRounds"] = args.max_rounds
        print(json.dumps(rpc("goal.create", payload, base=args.base), indent=2))
        return 0

    payload = {"sessionId": session_id,
               "ref": {k: current_goal(session_id, args.base)[k]
                       for k in ("id", "revision")}}
    if args.action == "edit":
        if args.objective is None and args.max_rounds is None:
            raise RpcError("goal edit requires --objective or --max-rounds")
        if args.objective is not None:
            payload["objective"] = args.objective
        if args.max_rounds is not None:
            payload["maxGoalRounds"] = args.max_rounds
    print(json.dumps(rpc(f"goal.{args.action}", payload, base=args.base), indent=2))
    return 0


def send_prompt(session_id: str, text: str, base: str, mode: str) -> None:
    rpc("session.prompt", {
        "sessionId": session_id,
        "mode": mode,
        "content": [{"type": "text", "text": text}],
    }, base=base)


def head_seq(session_id: str, base: str) -> int:
    value = rpc("session.history", {"sessionId": session_id, "maxMessages": 1}, base=base)
    return (value.get("projections") or {}).get("asOfSeq", -1)


def wait_for_turn_end(session_id: str, base: str, after_seq: int,
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
    while time.time() < deadline:
        time.sleep(poll)
        try:
            summary = find_session(session_id, base)
        except RpcError:
            continue
        running = bool(summary.get("running"))
        saw_running = saw_running or running
        for event in reversed(fetch_events(session_id, base, 4)):
            if event.get("type") == "turn/end" and event.get("seq", -1) > after_seq:
                return True, {"reason": (event.get("data") or {}).get("reason"),
                              "seq": event.get("seq")}
        if watch_pending and running:
            blocked = [item for item in pending_requests(base, 0.8, 4.0)
                       if item.get("sessionId") == session_id]
            if blocked:
                return False, {"blocked": True, "pending": blocked,
                               "hint": "answer with dsh_answer (questions) or "
                                       "dsh_approve (approvals), then wait again"}
        # Idle again without ever going busy and no new turn: nothing was queued.
        if saw_running and not running:
            continue
    return False, {"timedOut": True, "waited": timeout}


def cmd_task(args) -> int:
    """Fire-and-forget: queue a long task, return immediately."""
    summary = find_session(args.session, args.base)
    before = head_seq(summary["sessionId"], args.base)
    send_prompt(summary["sessionId"], args.prompt, args.base, args.mode)
    print(json.dumps({
        "dispatched": True,
        "sessionId": summary["sessionId"],
        "mode": args.mode,
        "seqBefore": before,
        "hint": f"poll: dshctl status {summary['sessionId'][:16]}",
    }, indent=2))
    return 0


def cmd_ask(args) -> int:
    """Short-form Q&A: prompt, block until the turn ends, print the reply."""
    summary = find_session(args.session, args.base)
    session_id = summary["sessionId"]
    before = head_seq(session_id, args.base)
    started = time.time()
    send_prompt(session_id, args.prompt, args.base, args.mode)
    completed, info = wait_for_turn_end(
        session_id, args.base, before, args.timeout, args.poll, watch_pending=True)
    if info.get("blocked"):
        # The worker asked something mid-reply. Returning now, rather than
        # sitting out the timeout, is what lets the caller answer it.
        print(json.dumps({"sessionId": session_id, "completed": False,
                          "waitedSeconds": round(time.time() - started, 1),
                          **info}, indent=2))
        return 3
    seq, reply, reasoning = latest_reply(session_id, args.base, after_seq=before)
    print(json.dumps({
        "sessionId": session_id,
        "completed": completed,
        "waitedSeconds": round(time.time() - started, 1),
        "turnEnd": info,
        "replySeq": seq,
        "reply": reply[: args.chars],
        "reasoningChars": len(reasoning),
    }, indent=2))
    return 0 if completed else 2


def cmd_wait(args) -> int:
    """
    Block until this worker's turn ends.

    The default semantics are what make this usable as an orchestrator
    primitive. With no --after, a *running* worker is waited on for its NEXT
    turn/end, while an *idle* one reports its last turn/end immediately since
    there is nothing to wait for. Passing after_seq=-1 unconditionally -- the
    obvious implementation -- makes the running case match the PREVIOUS turn's
    end and return instantly, which reads as success while the worker is still
    mid-task.
    """
    summary = find_session(args.session, args.base)
    session_id = summary["sessionId"]

    if args.after is not None:
        after = args.after
    elif summary.get("running"):
        after = head_seq(session_id, args.base)
    else:
        last = last_turn_end(session_id, args.base)
        print(json.dumps({"completed": last is not None, "alreadyIdle": True,
                          **(last or {})}, indent=2))
        return 0 if last else 2

    completed, info = wait_for_turn_end(
        session_id, args.base, after, args.timeout, args.poll, watch_pending=True)
    if info.get("blocked"):
        print(json.dumps({"completed": False, "waitedFromSeq": after, **info}, indent=2))
        return 3
    print(json.dumps({"completed": completed, "waitedFromSeq": after,
                      "kind": (info.get("reason") or {}).get("kind"),
                      **info}, indent=2))
    return 0 if completed else 2


def cmd_create(args) -> int:
    payload: dict = {}
    if args.cwd:
        payload["cwd"] = args.cwd
    if args.preset:
        payload["agentPreset"] = args.preset
    if args.session_id:
        payload["sessionId"] = args.session_id
    if not payload:
        raise RpcError("refusing to create a session with no cwd/preset "
                       "(every field is optional server-side, so an empty "
                       "payload silently creates a blank session)")
    value = rpc("session.create", payload, base=args.base)
    if args.provider and args.model:
        rpc("session.selectModel", {
            "sessionId": value["sessionId"],
            "provider": args.provider,
            "model": args.model,
        }, base=args.base)
        value["selectedModel"] = f"{args.provider}/{args.model}"
    if args.permission:
        # Applied before the first prompt, so no call ever runs under the default.
        value["permission"] = set_permission(value["sessionId"], args.permission, args.base)
    print(json.dumps(value, indent=2))
    return 0


def cmd_models(args) -> int:
    summary = find_session(args.session, args.base)
    value = rpc("session.models", {"sessionId": summary["sessionId"]}, base=args.base)
    catalog = []
    for group in value.get("groups", []):
        for model in group.get("models", []):
            reasoning = model.get("reasoning") or {}
            catalog.append({
                "provider": group.get("id"),
                "model": model.get("id"),
                "name": model.get("name"),
                "reasoningEfforts": [e.get("id") for e in reasoning.get("efforts", [])] or None,
                "defaultEffort": reasoning.get("defaultEffort"),
            })
    print(json.dumps({"current": value.get("current"),
                      "routable": value.get("routable"),
                      "catalog": catalog,
                      "failures": value.get("failures")}, indent=2))
    return 0


def cmd_cancel(args) -> int:
    """
    Interrupt a running turn, and report what actually happened.

    `session.cancel` returns `{accepted: true}` unconditionally — including for a
    session that is already idle. Measured: cancelling an idle session and
    cancelling a mid-flight one produce byte-identical receipts. So the receipt
    is not evidence, and an orchestrator that trusts it can believe it stopped
    work that had in fact already finished and committed its side effects.

    The real evidence is the turn's own ending: a genuine interruption lands a
    `turn/end` with kind `aborted` and reason `user`. This waits for that and
    reports it, so a no-op cancel is visibly a no-op.
    """
    summary = find_session(args.session, args.base)
    session_id = summary["sessionId"]

    if not summary.get("running"):
        # Nothing to interrupt. Say so rather than passing on a hollow receipt.
        print(json.dumps({
            "cancelled": False,
            "reason": "session was already idle — nothing was running to cancel",
            "lastTurnEnd": last_turn_end(session_id, args.base),
        }, indent=2))
        return 0

    before = head_seq(session_id, args.base)
    receipt = rpc("session.cancel", {"sessionId": session_id}, base=args.base)
    completed, info = wait_for_turn_end(
        session_id, args.base, before, timeout=args.timeout, poll=2.0)
    end = info.get("turnEnd") or last_turn_end(session_id, args.base)
    kind = (end or {}).get("kind")
    print(json.dumps({
        # `aborted` is the abort landing; any other kind means the turn ended on
        # its own terms and the cancel raced it.
        "cancelled": kind == "aborted",
        "receipt": receipt,
        "turnEnd": end,
        **({} if completed else
           {"note": f"turn had not ended within {args.timeout}s; "
                    f"the worker may be in a non-interruptible step"}),
        **({"note": f"turn ended as {kind!r}, not 'aborted' — it finished before "
                    f"the cancel took effect, so its work stands"}
           if completed and kind not in ("aborted", None) else {}),
    }, indent=2))
    return 0


def cmd_name(args) -> int:
    """
    Pin a human-meaningful title on a session.

    Titles are otherwise auto-generated from the conversation, which makes a
    roster of long-lived workers read as near-identical restatements of the same
    project. An explicit rename appends a `session/title` event with the `user`
    source, which *pins* it: in-flight automatic generation is superseded and
    later messages schedule none. So a name the orchestrator assigns stays put
    for the life of the session, which is the property that makes it usable as
    an identifier rather than a description.

    Normalisation (packages/session/session-title/src/normalize.ts) strips ANSI
    and control sequences, collapses whitespace to one line, and truncates to a
    UTF-8 byte budget (80 in the base bundle) without splitting a code point.
    A title that normalises to empty is rejected as `title-invalid`, so the
    accepted string is echoed back rather than assumed.
    """
    summary = find_session(args.session, args.base)
    result = rpc("session.rename",
                 {"sessionId": summary["sessionId"], "title": args.title},
                 base=args.base)
    accepted = result.get("title")
    print(json.dumps({
        "sessionId": summary["sessionId"],
        "title": accepted,
        "seq": result.get("seq"),
        # Surfaced so a silent truncation or strip is visible to the caller.
        **({"note": f"normalised from {args.title!r} (title bytes are capped)"}
           if accepted != args.title else {}),
    }, indent=2))
    return 0


# ---------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dshctl", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE)
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    p = subparsers.add_parser("list", help="roster of worker sessions")
    p.add_argument("--all", action="store_true", help="include blank sessions")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_list)

    p = subparsers.add_parser("status", help="stats, context pressure, latest reply")
    p.add_argument("session")
    p.add_argument("--chars", type=int, default=2000)
    p.add_argument("--reasoning", action="store_true",
                   help="also show the model's private reasoning blocks")
    p.set_defaults(func=cmd_status)

    p = subparsers.add_parser("grep", help="regex search one session's history")
    p.add_argument("session")
    p.add_argument("pattern")
    p.add_argument("--max-messages", type=int, default=200)
    p.add_argument("--pages", type=int, default=1,
                   help="history pages to walk backwards (default 1 = recent window)")
    p.add_argument("--context", type=int, default=120)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--case-sensitive", action="store_true")
    p.set_defaults(func=cmd_grep)

    p = subparsers.add_parser("search", help="server-side search across all sessions")
    p.add_argument("query")
    p.add_argument("--chars", type=int, default=4000)
    p.set_defaults(func=cmd_search)

    p = subparsers.add_parser("task", help="dispatch a long task, return immediately")
    p.add_argument("session")
    p.add_argument("prompt")
    p.add_argument("--mode", choices=("queue", "steer"), default="queue")
    p.set_defaults(func=cmd_task)

    p = subparsers.add_parser("ask", help="short-form Q&A, blocks for the reply")
    p.add_argument("session")
    p.add_argument("prompt")
    p.add_argument("--mode", choices=("queue", "steer"), default="queue")
    p.add_argument("--timeout", type=float, default=300.0)
    p.add_argument("--poll", type=float, default=3.0)
    p.add_argument("--chars", type=int, default=8000)
    p.set_defaults(func=cmd_ask)

    p = subparsers.add_parser("wait", help="block until the current turn ends")
    p.add_argument("session")
    p.add_argument("--after", type=int, default=None)
    p.add_argument("--timeout", type=float, default=1800.0)
    p.add_argument("--poll", type=float, default=5.0)
    p.set_defaults(func=cmd_wait)

    p = subparsers.add_parser("pending",
                              help="questions/approvals workers are blocked on")
    p.add_argument("session", nargs="?", default=None,
                   help="optional: only this session's pending requests")
    p.add_argument("--settle", type=float, default=1.5,
                   help="seconds of stream silence that means the baseline is in")
    p.add_argument("--timeout", type=float, default=8.0)
    p.set_defaults(func=cmd_pending)

    p = subparsers.add_parser("answer", help="answer a worker's pending question")
    p.add_argument("session")
    p.add_argument("rpc_id", help="rpcId from `pending` — echoed, never minted")
    p.add_argument("--answer", action="append", metavar="QID=LABEL",
                   help="repeatable; one ask() takes one batch covering every question")
    p.add_argument("--answers", metavar="JSON",
                   help='full batch: [{"id":"q1","selected":["Yes"],"custom":"..."}]')
    p.add_argument("--custom", help="free-text 'Other' on the last --answer")
    p.set_defaults(func=cmd_answer)

    p = subparsers.add_parser("goal", help="read or revise the persisted objective")
    # Session first, matching every other subcommand. The reverse order was an
    # inconsistency that reliably produced a confusing "invalid choice" error.
    p.add_argument("session")
    p.add_argument("action", nargs="?", default="show",
                   choices=("show", "set", "edit", "pause", "resume",
                            "complete", "clear"))
    p.add_argument("--objective", help="required for set, optional for edit")
    p.add_argument("--max-rounds", type=int, default=None, dest="max_rounds")
    p.set_defaults(func=cmd_goal)

    p = subparsers.add_parser("create", help="create a worker session")
    p.add_argument("--cwd")
    p.add_argument("--preset")
    p.add_argument("--session-id")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--permission",
                   help="permission preset to apply before anything is dispatched "
                        "(e.g. supervised, autonomous, read-only)")
    p.set_defaults(func=cmd_create)

    p = subparsers.add_parser("models", help="model catalog for a session")
    p.add_argument("session")
    p.set_defaults(func=cmd_models)

    p = subparsers.add_parser("cancel", help="cancel the running turn")
    p.add_argument("session")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="seconds to wait for the abort to actually land")
    p.set_defaults(func=cmd_cancel)

    p = subparsers.add_parser("approve", help="grant or reject a pending tool approval")
    p.add_argument("session")
    p.add_argument("rpc_id")
    p.add_argument("outcome", choices=("allowed-once", "rejected"))
    p.set_defaults(func=cmd_approve)

    p = subparsers.add_parser("permission", help="show or set a worker's permission preset")
    p.add_argument("session")
    p.add_argument("preset", nargs="?", help="omit to show the current preset")
    p.add_argument("--force", action="store_true", help="switch even while mid-turn")
    p.set_defaults(func=cmd_permission)

    p = subparsers.add_parser("name", help="pin a title on a session")
    p.add_argument("session")
    p.add_argument("title", help="normalised to one line, capped at 80 UTF-8 bytes")
    p.set_defaults(func=cmd_name)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
