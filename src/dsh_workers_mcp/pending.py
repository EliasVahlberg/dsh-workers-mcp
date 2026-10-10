"""
What a blocked worker waits on: the pending questions and approvals, how to
read an approval, and how to check an answer before it is sent.
"""

from __future__ import annotations

import json

from dsh_workers_mcp import sessions
from dsh_workers_mcp.transport import DshClient, RpcError

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

def pending_requests(client: DshClient, settle: float, max_seconds: float) -> list[dict]:
    """Every outstanding question and approval, keyed by the rpcId to echo."""
    out: dict[str, dict] = {}
    for frame in client.stream("/api/events.mux", settle, max_seconds):
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


def tool_call_arguments(session_id: str, call_id: str, client: DshClient) -> object:
    """
    The arguments of one tool call, found by callId in the session log.

    An approval request deliberately omits arguments (dsh attaches the prompt to
    the already-streamed call instead of duplicating it), so without this join
    an approver sees only a tool name and a reason -- and the reason is usually
    model-written. The arguments are what is actually about to run.
    """
    for event in reversed(sessions.fetch_events(session_id, client, 12)):
        data = event.get("data") or {}
        if event.get("type") == "tool/call" and data.get("callId") == call_id:
            raw = data.get("arguments")
            try:
                return json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                return raw
    return None


def describe_approval(item: dict, client: DshClient) -> dict:
    """Add the tool arguments and say who wrote the reason."""
    reason = item.get("reason") or ""
    out = dict(item)
    if item.get("callId"):
        out["arguments"] = tool_call_arguments(item["sessionId"], item["callId"], client)
    # tool-policy reasons come from our own config; everything else here --
    # notably a sandbox escalation's justification -- is text the worker's model
    # wrote, and a hijacked worker controls it.
    out["reasonAuthor"] = ("tool-policy rule (trusted config)"
                           if TOOL_POLICY_MARK in reason
                           else "worker model (untrusted -- judge the arguments, not the prose)")
    return out


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

    for index, (answer, question) in enumerate(zip(answers, questions, strict=True)):
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


