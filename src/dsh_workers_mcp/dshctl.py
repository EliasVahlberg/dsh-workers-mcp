#!/usr/bin/env python3
"""
dshctl — orchestrator-side control of dsh worker sessions over the live
`dsh web` server's HTTP API.

This module is the command line only: the parser and `main`. The commands are in
`cmd_sessions` and `cmd_work`, and the logic they use is in `sessions`,
`requests`, `permissions`, `waiting` and `models`. The wire format is in
`transport`.
"""

from __future__ import annotations

import argparse
import sys

from dsh_workers_mcp.cmd_sessions import (
    cmd_cancel,
    cmd_create,
    cmd_fork,
    cmd_goal,
    cmd_grep,
    cmd_list,
    cmd_models,
    cmd_name,
    cmd_permission,
    cmd_policy_patch,
    cmd_search,
    cmd_status,
)
from dsh_workers_mcp.cmd_work import cmd_answer, cmd_approve, cmd_ask, cmd_pending, cmd_task, cmd_wait
from dsh_workers_mcp.shape import CheckedClient
from dsh_workers_mcp.transport import DEFAULT_BASE, HttpClient, RpcError

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

    p = subparsers.add_parser("wait", help="block until the current turn ends "
                              "(several sessions: the first of them)")
    p.add_argument("session", nargs="+")
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
    p.add_argument("--start", action="store_true",
                   help="required for set: the goal loop starts at once")
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
    p.add_argument("--reasoning-effort", dest="reasoning_effort")
    p.set_defaults(func=cmd_create)

    p = subparsers.add_parser("fork", help="fork a session at a turn boundary")
    p.add_argument("session")
    p.add_argument("--after-turn", type=int, default=None, dest="after_turn")
    p.add_argument("--at-seq", type=int, default=None, dest="at_seq")
    p.set_defaults(func=cmd_fork)

    p = subparsers.add_parser("models", help="model catalog for a session")
    p.add_argument("session")
    p.add_argument("--provider")
    p.add_argument("--model")
    p.add_argument("--reasoning-effort", dest="reasoning_effort")
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

    p = subparsers.add_parser("policy-patch", help="print or write the tool-policy dsh patch "
                              "(supervision-level presets), with this install's plugin path")
    p.add_argument("--out", help="write here instead of stdout")
    p.add_argument("--force", action="store_true", help="overwrite --out if it exists")
    p.set_defaults(func=cmd_policy_patch)

    p = subparsers.add_parser("name", help="pin a title on a session")
    p.add_argument("session")
    p.add_argument("title", help="normalised to one line, capped at 80 UTF-8 bytes")
    p.set_defaults(func=cmd_name)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.client = CheckedClient(HttpClient(args.base))
    try:
        return args.func(args)
    except RpcError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        for line in args.client.drift_lines():
            print(f"warning: the dsh API differs from the tested shape: {line}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
