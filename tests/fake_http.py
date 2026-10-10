"""
A real HTTP and WebSocket server in front of a `FakeDsh`, for tests that exercise
`HttpClient` and the programs built on it. It speaks the wire format that the
transport notes in `transport.py` describe. It listens on a free loopback port.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fakes import FakeDsh

from dsh_workers_mcp.transport import RpcError


def _text_frame(text: str) -> bytes:
    payload = text.encode()
    if len(payload) < 126:
        return bytes([0x81, len(payload)]) + payload
    return bytes([0x81, 126]) + len(payload).to_bytes(2, "big") + payload


class FakeHttpDsh:
    """Serve `fake` over HTTP on 127.0.0.1. Use as a context manager."""

    def __init__(self, fake: FakeDsh) -> None:
        self.fake = fake
        self.http_error: tuple[int, str] | None = None     # answer every POST with this status
        self.refuse_upgrade = False                         # answer a WebSocket GET with 426
        self.upgrade_delay = 0.0                            # seconds to wait before the 101 answer
        self.requests: list[str] = []                       # paths seen, in order
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:           # keep test output quiet
                pass

            def do_POST(self) -> None:                      # noqa: N802 - the http.server name
                outer.requests.append(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
                if outer.http_error:
                    status, text = outer.http_error
                    self._send(status, text.encode())
                    return
                path = self.path.removeprefix("/api/")
                if path in ("respond", "commands/execute"):
                    reply = outer.fake.post(self.path, body, path)
                else:
                    reply = self._rpc(path, body)
                self._send(200, json.dumps(reply).encode())

            def _rpc(self, method: str, body: dict) -> dict:
                envelope = {"type": "server-response", "rpcId": body.get("rpcId")}
                try:
                    value = outer.fake.rpc(method, body.get("payload") or {})
                except RpcError as exc:
                    return {**envelope, "result": {"ok": False, "error": str(exc)}}
                return {**envelope, "result": {"ok": True, "value": value}}

            def do_GET(self) -> None:                       # noqa: N802
                outer.requests.append(self.path)
                if outer.refuse_upgrade or self.headers.get("Upgrade", "").lower() != "websocket":
                    self._send(426, b"upgrade required")
                    return
                time.sleep(outer.upgrade_delay)
                self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                 b"Connection: Upgrade\r\n\r\n")
                for frame in outer.fake.frames:
                    self.wfile.write(_text_frame(json.dumps(frame)))
                self.wfile.write(bytes([0x88, 0]))          # close
                self.wfile.flush()

            def _send(self, status: int, data: bytes) -> None:
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> FakeHttpDsh:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
