"""The dshctl commands that inspect and set up sessions (list, status, search, create, fork, ...)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from dsh_workers_mcp import models, permissions, sessions, waiting
from dsh_workers_mcp.transport import DshClient, RpcError

# ------------------------------------------------------------------- commands

def cmd_list(args) -> int:
    items = args.client.rpc("session.list").get("items", [])
    items.sort(key=lambda s: s.get("updatedAt") or 0, reverse=True)
    rows = []
    for summary in items:
        if summary.get("blank") and not args.all:
            continue
        values = sessions.projections(summary)
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
    summary = sessions.find_session(args.session, args.client)
    values = sessions.projections(summary)
    stats = values.get("sessionStats") or {}
    usage = values.get("tokenUsage") or {}
    pressure = values.get("contextPressure") or {}
    window = pressure.get("contextWindow") or 0
    used = pressure.get("pressureTokens") or 0
    cache_read = usage.get("cacheReadTokens") or 0
    uncached = usage.get("uncachedInputTokens") or 0
    seq, reply, reasoning = sessions.latest_reply(summary["sessionId"], args.client)

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
        "lastTurnEnd": sessions.last_turn_end(summary["sessionId"], args.client),
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_grep(args) -> int:
    summary = sessions.find_session(args.session, args.client)
    pattern = re.compile(args.pattern, 0 if args.case_sensitive else re.IGNORECASE)
    events, exhausted = sessions.walk_events(summary["sessionId"], args.client,
                                    args.max_messages, args.pages)
    hits = []
    for event in events:
        body = sessions.text_of(event)
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
    value = args.client.rpc("session.search", {"query": args.query})
    print(json.dumps(value, indent=2)[: args.chars])
    return 0


def cmd_policy_patch(args) -> int:
    rendered = permissions.render_policy_patch()
    if not args.out:
        sys.stdout.write(rendered)
        return 0
    target = Path(args.out).expanduser()
    if target.exists() and not args.force:
        raise RpcError(f"{target} exists; pass --force to overwrite it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {target}\ncheck it:  dsh --patch {target} --dump-config\n"
          f"use it:    dsh --patch {target} --profile <profile>", file=sys.stderr)
    return 0


def cmd_permission(args) -> int:
    session_id = sessions.find_session(args.session, args.client)["sessionId"]
    if args.preset:
        if sessions.running_now(session_id, args.client) and not args.force:
            raise RpcError("worker is mid-turn; the new preset would apply from its "
                           "next tool call. Pass --force to switch anyway.")
        out = permissions.set_permission(session_id, args.preset, args.client)
    else:
        out = permissions.get_permission(session_id, args.client)
    print(json.dumps({"sessionId": session_id, **out}, indent=2))
    return 0


# ------------------------------------------------------------------- goals
#
# The goal is the one piece of orientation that survives compaction as
# structured state rather than as prose in a summary, which makes it the
# durable home for "what this worker is for". Every goal.* method is a
# mutation; current state is read only from the 'goal' session projection
# (packages/host/apiproxy/src/api/goals.schema.ts). Mutations carry a
# {id, revision} ref, so a stale revision is rejected rather than silently
# clobbering a concurrent edit.

def current_goal(session_ref: str, client: DshClient) -> dict:
    goal = sessions.projections(sessions.find_session(session_ref, client)).get("goal") or {}
    goal = goal.get("goal") or {}
    if not goal.get("id"):
        raise RpcError("session has no goal — create one with `goal set`")
    return goal


def cmd_goal(args) -> int:
    session_id = sessions.find_session(args.session, args.client)["sessionId"]

    if args.action == "show":
        print(json.dumps(sessions.projections(sessions.find_session(session_id, args.client)).get("goal"),
                         indent=2))
        return 0

    if args.action == "set":
        if not args.objective:
            raise RpcError("goal set requires --objective")
        if not getattr(args, "start", False):
            raise RpcError(
                "goal set starts an autonomous goal loop at once: dsh creates every "
                "goal active, and its round driver begins round 1 on an idle worker "
                "with no task (maxGoalRounds defaults to 256). It is not a place for "
                "standing orientation; use the brief for that. Pass start=true only "
                "to hand the worker an objective to pursue unattended.")
        payload: dict = {"sessionId": session_id, "objective": args.objective}
        if args.max_rounds is not None:
            payload["maxGoalRounds"] = args.max_rounds
        print(json.dumps(args.client.rpc("goal.create", payload), indent=2))
        return 0

    payload = {"sessionId": session_id,
               "ref": {k: current_goal(session_id, args.client)[k]
                       for k in ("id", "revision")}}
    if args.action == "edit":
        if args.objective is None and args.max_rounds is None:
            raise RpcError("goal edit requires --objective or --max-rounds")
        if args.objective is not None:
            payload["objective"] = args.objective
        if args.max_rounds is not None:
            payload["maxGoalRounds"] = args.max_rounds
    print(json.dumps(args.client.rpc(f"goal.{args.action}", payload), indent=2))
    return 0


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
    value = args.client.rpc("session.create", payload)
    effort = getattr(args, "reasoning_effort", None)
    if (args.provider and args.model) or effort:
        value.update(models.select_model(value["sessionId"], args.client, args.provider,
                                  args.model, effort))
    if args.permission:
        # Applied before the first prompt, so no call ever runs under the default.
        value["permission"] = permissions.set_permission(value["sessionId"], args.permission, args.client)
    print(json.dumps(value, indent=2))
    return 0


def cmd_fork(args) -> int:
    """
    Fork a session into a new one that keeps its history up to a turn boundary.

    dsh forks on whole turns: an anchor inside a turn keeps that entire turn.
    So `after_turn=N` (keep turns 1..N) is the unambiguous form; `at_seq`
    passes dsh's own anchor through. Neither: up to the last completed turn.
    """
    source = sessions.find_session(args.session, args.client)["sessionId"]
    payload: dict = {"sessionId": source}
    if args.after_turn is not None:
        payload["atSeq"] = waiting.turn_end_seq(source, args.after_turn, args.client)
    elif args.at_seq is not None:
        payload["atSeq"] = args.at_seq
    value = args.client.rpc("session.fork", payload)
    print(json.dumps({"sessionId": value["sessionId"], "forkedFrom": source,
                      **({"atSeq": payload["atSeq"]} if "atSeq" in payload else {})},
                     indent=2))
    return 0


def cmd_models(args) -> int:
    summary = sessions.find_session(args.session, args.client)
    effort = getattr(args, "reasoning_effort", None)
    if effort or getattr(args, "model", None):
        if summary.get("running"):
            raise RpcError("the worker is mid-turn; change its model or effort when it is idle")
        print(json.dumps({"sessionId": summary["sessionId"],
                          **models.select_model(summary["sessionId"], args.client,
                                         getattr(args, "provider", None),
                                         getattr(args, "model", None), effort)}, indent=2))
        return 0
    value = args.client.rpc("session.models", {"sessionId": summary["sessionId"]})
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
    summary = sessions.find_session(args.session, args.client)
    session_id = summary["sessionId"]

    if not summary.get("running"):
        # Nothing to interrupt. Say so rather than passing on a hollow receipt.
        print(json.dumps({
            "cancelled": False,
            "reason": "session was already idle — nothing was running to cancel",
            "lastTurnEnd": sessions.last_turn_end(session_id, args.client),
        }, indent=2))
        return 0

    before = waiting.head_seq(session_id, args.client)
    receipt = args.client.rpc("session.cancel", {"sessionId": session_id})
    completed, info = waiting.wait_for_turn_end(
        session_id, args.client, before, timeout=args.timeout, poll=2.0)
    end = info.get("turnEnd") or sessions.last_turn_end(session_id, args.client)
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
    summary = sessions.find_session(args.session, args.client)
    result = args.client.rpc("session.rename",
                             {"sessionId": summary["sessionId"], "title": args.title})
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


