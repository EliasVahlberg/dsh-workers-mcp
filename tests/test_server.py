"""
MCP protocol-surface tests. No live dsh server and no socket: every RPC failure
is injected, so these assert the transport contract rather than dsh's behaviour.

The invariant that matters most here: a tool failure must come back as an
``isError`` result, never as a raised exception, because an exception escaping
``handle()`` would kill the stdio transport and take every other tool with it.
"""

from __future__ import annotations

import pytest

from dsh_workers_mcp import dshctl, server


def _call(name: str, arguments: dict | None = None, request_id: int = 1) -> dict:
    return server.handle({
        "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {}},
    })


# ------------------------------------------------------------------ handshake

def test_initialize_advertises_tools() -> None:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert "tools" in response["result"]["capabilities"]
    assert response["result"]["serverInfo"]["name"] == "dsh-workers"


def test_initialized_notification_gets_no_response() -> None:
    """A notification has no id; replying to one is a protocol violation."""
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unsupported_notification_gets_no_response() -> None:
    assert server.handle({"jsonrpc": "2.0", "method": "some/unknown"}) is None


def test_unsupported_request_gets_an_error() -> None:
    response = server.handle({"jsonrpc": "2.0", "id": 9, "method": "some/unknown"})
    assert response["error"]["code"] == -32601


# ------------------------------------------------------------------ tool list

def test_every_dispatchable_tool_is_declared() -> None:
    """A tool in DISPATCH but absent from tools/list is invisible to a client."""
    declared = {tool["name"] for tool in server._tools()}
    assert declared == set(server.DISPATCH) | set(server.STATE_TOOLS)


def test_tools_list_covers_the_expected_surface() -> None:
    declared = {tool["name"] for tool in server._tools()}
    assert declared == {
        # observe
        "dsh_list", "dsh_status", "dsh_grep", "dsh_search", "dsh_models",
        # delegate
        "dsh_task", "dsh_ask", "dsh_wait",
        # unblock
        "dsh_pending", "dsh_answer", "dsh_approve",
        # manage
        "dsh_create", "dsh_cancel", "dsh_goal", "dsh_name", "dsh_permission",
        # orchestrator state (only listed when a state dir is configured)
        "dsh_roster", "dsh_tasks",
    }


def test_every_tool_declares_a_schema_and_description() -> None:
    for tool in server._tools():
        assert tool["description"].strip(), tool["name"]
        assert tool["inputSchema"]["type"] == "object", tool["name"]


def test_session_taking_tools_require_session() -> None:
    needs_session = {"dsh_status", "dsh_grep", "dsh_task", "dsh_ask",
                     "dsh_wait", "dsh_models", "dsh_cancel", "dsh_answer",
                     "dsh_goal"}
    for tool in server._tools():
        if tool["name"] in needs_session:
            assert "session" in tool["inputSchema"].get("required", []), tool["name"]


def test_pending_does_not_require_a_session() -> None:
    """Its whole purpose is the cross-session snapshot; session is a filter."""
    pending = next(t for t in server._tools() if t["name"] == "dsh_pending")
    assert "session" not in pending["inputSchema"].get("required", [])


# --------------------------------------------------------------- error paths

def test_unknown_tool_is_a_jsonrpc_error() -> None:
    assert _call("dsh_nonexistent")["error"]["code"] == -32601


def test_rpc_failure_becomes_an_is_error_result(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise dshctl.RpcError("cannot reach 127.0.0.1:3080 — is `dsh web` running?")

    monkeypatch.setattr(dshctl, "rpc", boom)
    response = _call("dsh_list")
    assert response["result"]["isError"] is True
    assert "dsh web" in response["result"]["content"][0]["text"]
    assert "error" not in response  # a tool failure is a result, not an RPC error


def test_unexpected_exception_is_contained(monkeypatch, capsys) -> None:
    """An escaping exception would kill the stdio transport for every tool."""
    def boom(*_a, **_k):
        raise ValueError("unexpected")

    monkeypatch.setattr(dshctl, "rpc", boom)
    response = _call("dsh_list")
    assert response["result"]["isError"] is True
    assert "internal error" in response["result"]["content"][0]["text"]


def test_missing_required_argument_is_contained() -> None:
    """A client that omits `session` must get a result, not a crash."""
    response = _call("dsh_status")
    assert response["result"]["isError"] is True


@pytest.mark.parametrize("name", [
    "dsh_list", "dsh_status", "dsh_grep", "dsh_search", "dsh_task", "dsh_ask",
    "dsh_wait", "dsh_models", "dsh_create", "dsh_cancel", "dsh_pending",
    "dsh_answer", "dsh_goal",
])
def test_no_tool_raises_through_handle(name: str, monkeypatch) -> None:
    """
    Blanket contract: whatever the arguments, handle() returns a response.
    Both transports are stubbed so nothing touches the network.
    """
    def boom(*_a, **_k):
        raise dshctl.RpcError("stubbed")

    monkeypatch.setattr(dshctl, "rpc", boom)
    monkeypatch.setattr(dshctl, "read_stream", lambda *a, **k: [])
    response = _call(name, {})
    assert response is not None
    assert "result" in response or "error" in response


# ------------------------------------------------------- stdout discipline

def test_tool_output_is_captured_not_printed(monkeypatch, capsys) -> None:
    """
    dshctl commands print to stdout; stdout is the MCP transport. Anything a
    command prints must be captured into the result instead of corrupting the
    frame stream.
    """
    monkeypatch.setitem(server.DISPATCH, "dsh_list",
                        lambda a: server._capture(
                            lambda _args: print("payload"), server.Args()))
    response = _call("dsh_list")
    assert response["result"]["content"][0]["text"] == "payload"
    assert capsys.readouterr().out == ""
