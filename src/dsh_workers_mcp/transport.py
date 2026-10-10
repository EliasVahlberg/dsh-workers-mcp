"""
Transport to a running `dsh web` server: the only module that opens a socket.

Everything else talks to dsh through a `DshClient`, so the code above this layer
runs unchanged against a fake in tests, and a change in the wire format of dsh
has one place to land.

Transport notes (reverse-engineered from packages/host/apiproxy, verified live):
  * the endpoint is POST /api/<method>, NOT a single RPC endpoint with a method field
  * the envelope is {"type":"client-request","rpcId":<str>,"method":<str>,"payload":{...}}
    -- this is NOT JSON-RPC 2.0 (no "jsonrpc" field, "rpcId" not "id")
  * the response is {"type":"server-response","rpcId":...,"result":{"ok":bool,...}}
  * loopback requests are trusted with no credentials
  * `host.describe` returns a `version` field, but dsh sets it to the constant
    '0.0.1' (packages/host/apiproxy/src/api-proxy.ts). Do not use it to decide
    anything. The shape check in `shape.py` is how drift is detected.

The pending questions and approvals come on a WebSocket downlink, not SSE. See
`HttpClient.stream`.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any, Protocol

DEFAULT_BASE = "http://127.0.0.1:3080"


class RpcError(RuntimeError):
    pass


class DshClient(Protocol):
    """What the rest of the package needs from dsh. Three calls."""

    base: str

    def rpc(self, method: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        """One call to POST /api/<method>. Returns the `value`. Raises RpcError on any failure."""
        ...

    def post(self, path: str, body: dict, label: str, timeout: float = 30.0) -> dict:
        """POST a JSON body to `path` and return the parsed JSON response, unchecked.

        For the endpoints that do not use the `/api/<method>` envelope (respond, commands/execute).
        Raises RpcError on a transport failure, with `label` in the message.
        """
        ...

    def stream(self, path: str, settle: float = 1.5, max_seconds: float = 8.0) -> list[dict]:
        """Open a downlink, collect the JSON frames until it goes quiet, close it."""
        ...


class HttpClient:
    """`DshClient` over HTTP and a read-only WebSocket, stdlib only."""

    def __init__(self, base: str = DEFAULT_BASE) -> None:
        self.base = base

    def __repr__(self) -> str:
        return f"HttpClient({self.base!r})"

    def _open(self, path: str, body: dict, label: str, timeout: float) -> Any:
        request = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode(),
            headers={"content-type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode(errors="replace")
            raise RpcError(f"{label}: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RpcError(
                f"{label}: cannot reach {self.base} ({exc.reason}) — is `dsh web` running?") from exc

    def rpc(self, method: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        parsed = self._open(f"/api/{method}", {
            "type": "client-request",
            "rpcId": f"dshctl-{int(time.time() * 1000)}",
            "method": method,
            "payload": payload or {},
        }, method, timeout)
        result = parsed.get("result") or {}
        if not result.get("ok"):
            raise RpcError(f"{method}: {json.dumps(result.get('error'), indent=2)}")
        return result.get("value") or {}

    def post(self, path: str, body: dict, label: str, timeout: float = 30.0) -> dict:
        return self._open(path, body, label, timeout)

    def stream(self, path: str, settle: float = 1.5, max_seconds: float = 8.0) -> list[dict]:
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
        sock, reader = _ws_connect(path, self.base, settle)       # raises on failure
        try:
            for text in _ws_frames(reader, deadline):
                try:
                    frames.append(json.loads(text))
                except json.JSONDecodeError:
                    continue                                       # one corrupt frame must not kill the read
        except (TimeoutError, OSError):
            pass                                                   # the stream went quiet: the baseline is in
        finally:
            reader.close()
            sock.close()
        return frames


# ------------------------------------------------- WebSocket (read-only)
#
# The running `dsh web` server serves its downlinks as WebSocket UPGRADES, not
# SSE -- a plain GET answers `426 upgrade required`
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
    while True:                                  # drain the response headers
        line = reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
    # The Sec-WebSocket-Accept key is not checked: the server is on loopback.
    return sock, reader


def _ws_frames(reader, deadline: float) -> Iterator[str]:
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
        if opcode == 0x8:                        # close
            return
        if opcode in (0x9, 0xA):                 # ping/pong: nothing to do on a short read
            continue
        if opcode in (0x0, 0x1):                 # continuation or text
            pending += payload.decode("utf-8", "replace")
            if fin:
                yield pending
                pending = ""
