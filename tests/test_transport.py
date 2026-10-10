"""`HttpClient` against a real local HTTP and WebSocket server: the wire paths that no fake can reach."""
from __future__ import annotations

import pytest
from fake_http import FakeHttpDsh
from fakes import FakeDsh

from dsh_workers_mcp.transport import HttpClient, RpcError


@pytest.fixture
def served():
    fake = FakeDsh()
    fake.add("session-aaaa1111")
    with FakeHttpDsh(fake) as server:
        yield fake, server, HttpClient(server.base)


def test_rpc_sends_the_envelope_to_the_method_path_and_returns_the_value(served) -> None:
    fake, server, client = served
    assert client.rpc("session.list")["items"][0]["sessionId"] == "session-aaaa1111"
    assert server.requests == ["/api/session.list"]


def test_rpc_raises_on_an_ok_false_result(served) -> None:
    fake, _server, client = served
    fake.failing["session.list"] = "no such thing"
    with pytest.raises(RpcError, match="session.list.*no such thing"):
        client.rpc("session.list")


def test_rpc_names_the_method_and_status_of_an_http_error(served) -> None:
    _fake, server, client = served
    server.http_error = (500, "boom")
    with pytest.raises(RpcError, match=r"session.list: HTTP 500: boom"):
        client.rpc("session.list")


def test_post_returns_the_unchecked_response_and_labels_errors(served) -> None:
    _fake, server, client = served
    assert client.post("/api/respond", {"type": "client-response"}, "respond") == {"accepted": True}
    server.http_error = (503, "down")
    with pytest.raises(RpcError, match=r"respond: HTTP 503: down"):
        client.post("/api/respond", {}, "respond")


def test_post_to_an_unreachable_server_is_an_rpc_error_not_a_url_error() -> None:
    with pytest.raises(RpcError, match="respond: cannot reach"):
        HttpClient("http://127.0.0.1:9").post("/api/respond", {}, "respond", timeout=1.0)


def test_stream_reads_the_frames_the_server_sends_and_stops_at_close(served) -> None:
    fake, _server, client = served
    fake.frames = [{"rpcId": "r1", "payload": {"type": "question/requested", "sessionId": "s1"}},
                   {"rpcId": "r2", "payload": {"type": "x", "pad": "y" * 300}}]      # needs a 16-bit length
    frames = client.stream("/api/events.mux", settle=1.0, max_seconds=3.0)
    assert [f["rpcId"] for f in frames] == ["r1", "r2"]


def test_stream_raises_when_the_server_refuses_the_upgrade(served) -> None:
    _fake, server, client = served
    server.refuse_upgrade = True
    with pytest.raises(RpcError, match="upgrade refused"):
        client.stream("/api/events.mux", settle=1.0, max_seconds=2.0)
