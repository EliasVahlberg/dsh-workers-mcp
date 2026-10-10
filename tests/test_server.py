"""
MCP protocol-surface tests. No live dsh server and no socket: every RPC failure
is injected, so these assert the transport contract rather than dsh's behaviour.

The invariant that matters most here: a tool failure must come back as an
``isError`` result, never as a raised exception, because an exception escaping
``handle()`` would kill the stdio transport and take every other tool with it.
"""

from __future__ import annotations

import dataclasses

import pytest

from fakes import ScriptedClient

from dsh_workers_mcp import dshctl, server
from dsh_workers_mcp.tools import Args, Tool, capture


def _listed() -> list[dict]:
    """Every tool as the MCP tools/list answer shows it."""
    return [tool.listing() for tool in server.TOOLS.values()]


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

def test_every_tool_is_listed_to_the_client_and_has_a_handler(state) -> None:
    """A tool that the registry holds but tools/list omits is invisible to a client."""
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = {tool["name"] for tool in response["result"]["tools"]}
    assert listed == set(server.TOOLS)
    for name, tool in server.TOOLS.items():
        assert tool.name == name and callable(tool.handler), name


def test_state_tools_are_listed_only_with_a_state_directory(monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE", None)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = {tool["name"] for tool in response["result"]["tools"]}
    assert listed == {name for name, tool in server.TOOLS.items() if not tool.needs_state}
    assert {"dsh_roster", "dsh_tasks", "dsh_digest", "dsh_brief"}.isdisjoint(listed)
    refused = _call("dsh_roster")["result"]
    assert refused["isError"] is True and "state directory" in refused["content"][0]["text"]


def test_adding_a_tool_is_adding_one_object(monkeypatch) -> None:
    """The listing, the dispatch and the error handling all come from the registry."""
    toy = Tool(name="dsh_toy", description="A toy.", schema={"type": "object", "properties": {}},
               summary="a toy", blocks="no", handler=lambda ctx, a: f"hello {a.get('who', 'world')}")
    monkeypatch.setitem(server.TOOLS, "dsh_toy", toy)
    assert "dsh_toy" in {t["name"] for t in _listed()}
    assert _call("dsh_toy", {"who": "you"})["result"]["content"][0]["text"] == "hello you"


def test_every_tool_has_a_summary_and_a_blocks_note() -> None:
    for tool in server.TOOLS.values():
        assert tool.summary.strip() and tool.blocks.strip(), tool.name
        assert "\n" not in tool.summary and "|" not in tool.summary, tool.name   # it goes in a table


def test_tools_list_covers_the_expected_surface() -> None:
    declared = {tool["name"] for tool in _listed()}
    assert declared == {
        # observe
        "dsh_list", "dsh_status", "dsh_grep", "dsh_search", "dsh_models",
        # delegate
        "dsh_task", "dsh_ask", "dsh_wait",
        # unblock
        "dsh_pending", "dsh_answer", "dsh_approve",
        # manage
        "dsh_create", "dsh_cancel", "dsh_goal", "dsh_name", "dsh_permission", "dsh_fork",
        # orchestrator state (only listed when a state dir is configured)
        "dsh_roster", "dsh_tasks", "dsh_digest", "dsh_brief",
    }


def test_every_tool_declares_a_schema_and_description() -> None:
    for tool in _listed():
        assert tool["description"].strip(), tool["name"]
        assert tool["inputSchema"]["type"] == "object", tool["name"]


def test_session_taking_tools_require_session() -> None:
    needs_session = {"dsh_status", "dsh_grep", "dsh_task", "dsh_ask",
                     "dsh_models", "dsh_cancel", "dsh_answer", "dsh_goal"}
    for tool in _listed():
        if tool["name"] in needs_session:
            assert "session" in tool["inputSchema"].get("required", []), tool["name"]


def test_wait_takes_one_session_or_several() -> None:
    """dsh_wait's schema requires neither, so the handler has to refuse an empty call."""
    wait = next(t for t in _listed() if t["name"] == "dsh_wait")
    assert {"session", "sessions"} <= set(wait["inputSchema"]["properties"])
    response = _call("dsh_wait", {})
    assert response["result"]["isError"] is True
    assert "session" in response["result"]["content"][0]["text"]


def test_pending_does_not_require_a_session() -> None:
    """Its whole purpose is the cross-session snapshot; session is a filter."""
    pending = next(t for t in _listed() if t["name"] == "dsh_pending")
    assert "session" not in pending["inputSchema"].get("required", [])


# --------------------------------------------------------------- error paths

def test_unknown_tool_is_a_jsonrpc_error() -> None:
    assert _call("dsh_nonexistent")["error"]["code"] == -32601


def test_rpc_failure_becomes_an_is_error_result(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise dshctl.RpcError("cannot reach 127.0.0.1:3080 — is `dsh web` running?")

    monkeypatch.setattr(server, "CLIENT", ScriptedClient(rpc=boom))
    response = _call("dsh_list")
    assert response["result"]["isError"] is True
    assert "dsh web" in response["result"]["content"][0]["text"]
    assert "error" not in response  # a tool failure is a result, not an RPC error


def test_unexpected_exception_is_contained(monkeypatch, capsys) -> None:
    """An escaping exception would kill the stdio transport for every tool."""
    def boom(*_a, **_k):
        raise ValueError("unexpected")

    monkeypatch.setattr(server, "CLIENT", ScriptedClient(rpc=boom))
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

    monkeypatch.setattr(server, "CLIENT", ScriptedClient(rpc=boom, post=boom, stream=lambda path: []))
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
    original = server.TOOLS["dsh_list"]
    monkeypatch.setitem(server.TOOLS, "dsh_list", dataclasses.replace(
        original, handler=lambda ctx, a: capture(lambda _args: print("payload"), Args(ctx.client))))
    response = _call("dsh_list")
    assert response["result"]["content"][0]["text"] == "payload"
    assert capsys.readouterr().out == ""


def test_fork_after_turn_anchors_on_that_turns_end(monkeypatch, capsys) -> None:
    """dsh forks on whole turns, so after_turn resolves to that turn's turn/end seq."""
    import json as _json
    from dsh_workers_mcp import dshctl
    sent = []
    events = [{"event": {"type": "turn/end", "seq": s, "data": {"turn": t}}}
              for t, s in ((1, 40), (2, 95), (3, 180))]

    def rpc(method, payload=None, base=None):
        sent.append((method, payload))
        if method == "session.list":
            return {"items": [{"sessionId": "session-src"}]}
        if method == "session.history":
            return {"events": events, "hasMore": False}
        if method == "session.fork":
            return {"sessionId": "session-child"}
        raise AssertionError(method)

    client = ScriptedClient(rpc=rpc)
    args = type("A", (), dict(session="src", after_turn=2, at_seq=None, client=client))()
    assert dshctl.cmd_fork(args) == 0
    out = _json.loads(capsys.readouterr().out)
    assert out == {"sessionId": "session-child", "forkedFrom": "session-src", "atSeq": 95}
    assert ("session.fork", {"sessionId": "session-src", "atSeq": 95}) in sent
