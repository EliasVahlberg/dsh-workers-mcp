#!/usr/bin/env python3
"""
MCP stdio server exposing dshctl's worker-session operations to an
MCP-capable orchestrator agent.

Protocol: JSON-RPC 2.0 over newline-delimited stdin/stdout. stdout carries
protocol frames ONLY — all diagnostics go to stderr, or the transport breaks.

Tool surface is deliberately task-shaped rather than a 1:1 mirror of the ~40
server RPC methods: the orchestrator needs a handful of operations
(observe, search, delegate, converse), not the whole API. The tools themselves
are defined in `dsh_workers_mcp.tools`. This module speaks the protocol, applies
the policy, and records each call.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

from dsh_workers_mcp import __version__
from dsh_workers_mcp import state as state_mod
from dsh_workers_mcp.shape import CheckedClient
from dsh_workers_mcp.tools import Context, Tool, all_tools
from dsh_workers_mcp.transport import DEFAULT_BASE, DshClient, HttpClient, RpcError

# The orchestrator state directory (see state/__init__.py). Set by main(); None disables
# state recording entirely, which is also what a bare import / the tests get.
STATE: state_mod.State | None = None
# The client that the tools use. Tests replace it.
CLIENT: DshClient = CheckedClient(HttpClient())

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "dsh-workers", "version": __version__}

# The tools by name. A test adds a tool by adding an entry here.
TOOLS: dict[str, Tool] = {tool.name: tool for tool in all_tools()}

# Presets this server refuses to apply, unless started with --allow-full-access.
#
# The orchestrator's worker-facing tools are meant to be auto-approved, so the
# lead can answer and dispatch without the user clicking through every call.
# That is only safe if the one unconfined preset is out of its reach: enforced
# here, where a prompt injected into the lead cannot talk its way past it,
# rather than left as a line in its instructions.
FORBIDDEN_PRESETS = {"danger-full-access"}
# Host slash commands that change a session's supervision knobs. Refused as
# prompt text too: dsh documents a prompt that is exactly one '/…' text block
# as a host command, and routing has differed from that in practice.
_KNOB_COMMANDS = ("/permission", "/sandbox", "/approval")


def check_policy(name: str, arguments: dict) -> None:
    """Refuse a call that would widen a worker's permissions past the allowed set."""
    wanted = None
    if name == "dsh_permission":
        wanted = arguments.get("preset")
    elif name == "dsh_create":
        wanted = arguments.get("permission")
    if wanted in FORBIDDEN_PRESETS:
        raise RpcError(
            f"preset {wanted!r} is refused by this server (started without "
            f"--allow-full-access). It is the user's call: ask them to set it.")
    if name in ("dsh_task", "dsh_ask"):
        head = (arguments.get("prompt") or "").lstrip().lower()
        if head.startswith(_KNOB_COMMANDS):
            raise RpcError(
                "a prompt starting with a permission/sandbox/approval slash command is "
                "refused; use dsh_permission to change a worker's preset")



def call_tool(name: str, arguments: dict) -> str:
    """
    Run one tool, recording it in the state dir when one is configured.

    Recording must never turn a call that succeeded against dsh into a
    reported failure, or the orchestrator would retry work that already
    happened. So a state-layer failure after the call is appended to the
    result as a visible note, and logged, rather than raised.
    """
    tool = TOOLS[name]
    ctx = Context(CLIENT, STATE)
    if tool.needs_state:
        if STATE is None:
            raise RpcError(f"{name} needs a state directory; the server was "
                           f"started with --no-state")
        return tool.handler(ctx, arguments)
    check_policy(name, arguments)  # may refuse the call
    if STATE is not None:
        state_mod.before_call(STATE, name, arguments)  # may refuse the call
    text = tool.handler(ctx, arguments)
    if STATE is None:
        return text
    try:
        enriched = state_mod.after_call(STATE, name, arguments, text)
        if enriched is not None:
            text = json.dumps(enriched, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - see docstring
        traceback.print_exc(file=sys.stderr)
        text += (f"\n\n[state: the call succeeded, but recording it in "
                 f"{STATE.root} failed: {exc}]")
    return text


def _log(name: str, arguments: dict, text: str, ok: bool, started: float) -> None:
    if STATE is None:
        return
    try:
        STATE.log_call(name, arguments, text, ok, int((time.time() - started) * 1000))
    except Exception:  # noqa: BLE001 - logging must not break the transport
        traceback.print_exc(file=sys.stderr)


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": message}}


def _run_tool(name: str, arguments: dict) -> tuple[str, bool]:
    """Run one tool and return (text, ok). A failure is a result, never an exception."""
    started = time.time()
    try:
        text, ok = call_tool(name, arguments) or "(no output)", True
    except RpcError as exc:
        text, ok = f"error: {exc}", False
    except Exception:  # noqa: BLE001 - report, never kill the transport
        traceback.print_exc(file=sys.stderr)
        text, ok = f"internal error:\n{traceback.format_exc()}", False
    _log(name, arguments, text, ok, started)
    return text, ok


def _handle_tool_call(request_id: object, params: dict) -> dict:
    name = params.get("name")
    if name not in TOOLS:
        return _error(request_id, f"unknown tool {name}")
    text, ok = _run_tool(name, params.get("arguments") or {})
    return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": not ok})


def handle(request: dict) -> dict | None:
    """Return a JSON-RPC response, or None for notifications."""
    method = request.get("method")
    request_id = request.get("id")
    if method == "initialize":
        return _result(request_id, {"protocolVersion": PROTOCOL_VERSION,
                                    "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method == "tools/list":
        listed = [t.listing() for t in TOOLS.values() if STATE is not None or not t.needs_state]
        return _result(request_id, {"tools": listed})
    if method == "tools/call":
        return _handle_tool_call(request_id, request.get("params") or {})
    if method in ("notifications/initialized", "initialized") or request_id is None:
        return None
    return _error(request_id, f"unsupported method {method}")


def main(argv: list[str] | None = None) -> int:
    global STATE, CLIENT
    parser = argparse.ArgumentParser(prog="dsh-workers-mcp")
    parser.add_argument("--state-dir", help=(
        "orchestrator state directory (logs, history mirror, roster, tasks). "
        "Default: .orchestrator in the directory the server is started from. "
        "Created on the first tool call, not at startup."))
    parser.add_argument("--no-state", action="store_true",
                        help="record nothing; dsh_roster and dsh_tasks are unavailable")
    parser.add_argument("--allow-full-access", action="store_true", help=(
        "let dsh_permission / dsh_create apply danger-full-access. Off by default, "
        "so the worker tools can be auto-approved for an orchestrator"))
    parser.add_argument("--base", default=DEFAULT_BASE,
                        help="URL of the dsh web server (default: %(default)s)")
    args = parser.parse_args(argv)
    CLIENT = CheckedClient(HttpClient(args.base))
    if args.allow_full_access:
        FORBIDDEN_PRESETS.clear()
    if not args.no_state:
        STATE = state_mod.State(state_mod.resolve_state_dir(args.state_dir), client=CLIENT)
        print(f"dsh-workers: state dir {STATE.root}", file=sys.stderr)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            print(f"dsh-workers: bad JSON frame: {exc}", file=sys.stderr)
            continue
        response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
