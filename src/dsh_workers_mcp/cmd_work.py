"""The dshctl commands that move work: task, ask, wait, pending, approve, answer."""

from __future__ import annotations

import json
import time

from dsh_workers_mcp import pending, sessions, waiting
from dsh_workers_mcp.transport import RpcError

def cmd_pending(args) -> int:
    items = pending.pending_requests(args.client, args.settle, args.timeout)
    if args.session:
        wanted = sessions.find_session(args.session, args.client)["sessionId"]
        items = [i for i in items if i.get("sessionId") == wanted]
    items = [pending.describe_approval(i, args.client) if i["kind"] == "approval/requested" else i
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
    matches = [i for i in pending.pending_requests(args.client, 1.5, 8.0) if i["rpcId"] == args.rpc_id]
    if not matches:
        raise RpcError(f"rpcId {args.rpc_id!r} is not pending — already settled, "
                       f"or the turn was interrupted. Re-read `pending`.")
    item = matches[0]
    if item["kind"] != "approval/requested":
        raise RpcError(f"rpcId {args.rpc_id!r} is a {item['kind']}; answer questions "
                       f"with `answer`, not `approve`.")
    wanted = sessions.find_session(args.session, args.client)["sessionId"]
    if item["sessionId"] != wanted:
        raise RpcError(f"rpcId {args.rpc_id!r} belongs to {item['sessionId']}, not {wanted}")

    body = {
        "type": "client-response",
        "rpcId": args.rpc_id,
        "result": {"ok": True, "value": {
            "sessionId": item["sessionId"],
            "approvalId": item["approvalId"],
            "outcome": args.outcome,
        }},
    }
    receipt = args.client.post("/api/respond", body, "respond")
    print(json.dumps({"receipt": receipt, "outcome": args.outcome,
                      "toolName": item.get("toolName"),
                      "arguments": pending.describe_approval(item, args.client).get("arguments")},
                     indent=2))
    return 0 if receipt.get("accepted") else 2


def cmd_answer(args) -> int:
    """Settle a pending question by echoing its rpcId back to /api/respond."""
    session_id = sessions.find_session(args.session, args.client)["sessionId"]
    answers = pending.build_answers(args)

    # Validate against the live request rather than trusting the caller: the
    # rules depend on the question (multiSelect, the exact option labels), so
    # they cannot be checked from the answer alone.
    matches = [item for item in pending.pending_requests(args.client, 1.5, 8.0)
               if item["rpcId"] == args.rpc_id]
    if not matches:
        raise RpcError(
            f"rpcId {args.rpc_id!r} is not pending — it may have been answered "
            f"already, or the turn was interrupted. Re-read `pending`.")
    if matches[0]["kind"] != "question/requested":
        raise RpcError(
            f"rpcId {args.rpc_id!r} is a {matches[0]['kind']}, not a question. "
            f"This tool cannot grant tool approvals.")
    pending.validate_answers(answers, matches[0].get("questions") or [])

    body = {
        "type": "client-response",
        "rpcId": args.rpc_id,
        "result": {"ok": True, "value": {
            "sessionId": session_id,
            "answer": {"answers": answers},
        }},
    }
    receipt = args.client.post("/api/respond", body, "respond")
    # A late or duplicate answer comes back accepted:false reason:'not-pending'.
    return 0 if receipt.get("accepted") else 2


def cmd_task(args) -> int:
    """Fire-and-forget: queue a long task, return immediately."""
    summary = sessions.find_session(args.session, args.client)
    before = waiting.head_seq(summary["sessionId"], args.client)
    waiting.send_prompt(summary["sessionId"], args.prompt, args.client, args.mode)
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
    summary = sessions.find_session(args.session, args.client)
    session_id = summary["sessionId"]
    before = waiting.head_seq(session_id, args.client)
    started = time.time()
    waiting.send_prompt(session_id, args.prompt, args.client, args.mode)
    completed, info = waiting.wait_for_turn_end(
        session_id, args.client, before, args.timeout, args.poll, watch_pending=True)
    if info.get("blocked"):
        # The worker asked something mid-reply. Returning now, rather than
        # sitting out the timeout, is what lets the caller answer it.
        print(json.dumps({"sessionId": session_id, "completed": False,
                          "waitedSeconds": round(time.time() - started, 1),
                          **info}, indent=2))
        return 3
    seq, reply, reasoning = sessions.latest_reply(session_id, args.client, after_seq=before)
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
    refs = list(args.session) if isinstance(args.session, (list, tuple)) else [args.session]
    refs += [r for r in (getattr(args, "sessions", None) or []) if r not in refs]
    if len(refs) > 1:
        if args.after is not None:
            raise RpcError("--after applies to one session, not several")
        out = waiting.wait_for_any(refs, args.client, args.timeout, args.poll)
        print(json.dumps(out, indent=2))
        return 0 if out.get("completed") else (3 if out.get("blocked") else 2)

    summary = sessions.find_session(refs[0], args.client)
    session_id = summary["sessionId"]

    if args.after is not None:
        after = args.after
    elif summary.get("running"):
        after = waiting.head_seq(session_id, args.client)
    else:
        last = sessions.last_turn_end(session_id, args.client)
        print(json.dumps({"completed": last is not None, "alreadyIdle": True,
                          **(last or {})}, indent=2))
        return 0 if last else 2

    completed, info = waiting.wait_for_turn_end(
        session_id, args.client, after, args.timeout, args.poll, watch_pending=True)
    if info.get("blocked"):
        print(json.dumps({"completed": False, "waitedFromSeq": after, **info}, indent=2))
        return 3
    print(json.dumps({"completed": completed, "waitedFromSeq": after,
                      "kind": (info.get("reason") or {}).get("kind"),
                      **info}, indent=2))
    return 0 if completed else 2


