"""Test doubles for the dsh client. Nothing here opens a socket."""
from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from dsh_workers_mcp.transport import RpcError


class ScriptedClient:
    """A `DshClient` whose three calls are plain callables that a test supplies.

    A call without a callable fails the test, so a test cannot reach dsh by accident.
    """

    base = "fake://dsh"

    def __init__(self, rpc: Callable[..., Any] | None = None, post: Callable[..., Any] | None = None,
                 stream: Callable[..., Any] | None = None) -> None:
        self._rpc, self._post, self._stream = rpc, post, stream

    def rpc(self, method: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        if self._rpc is None:
            raise AssertionError(f"unexpected rpc {method}")
        return self._rpc(method, payload)

    def post(self, path: str, body: dict, label: str, timeout: float = 30.0) -> dict:
        if self._post is None:
            raise AssertionError(f"unexpected post {path}")
        return self._post(path, body)

    def stream(self, path: str, settle: float = 1.5, max_seconds: float = 8.0) -> list[dict]:
        if self._stream is None:
            raise AssertionError(f"unexpected stream {path}")
        return self._stream(path)


class FakeDsh:
    """An in-memory dsh that implements `DshClient`. Models sessions, history paging, prompts."""

    base = "fake://dsh"

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.frames: list[dict] = []
        self.failing: dict[str, str] = {}      # method -> error text: the call raises RpcError
        self.sessions: dict[str, dict] = {}
        self.events: dict[str, list[dict]] = {}
        self.prompts: list[tuple[str, str]] = []
        self.clock = 1_790_000_000_000

    def add(self, sid: str, title: str = "t", preset: str = "supervised") -> str:
        self.sessions[sid] = {"sessionId": sid, "running": False, "cwd": "/w",
                              "agentPreset": "standard", "blank": False,
                              "projections": {"values": {
                                  "title": title,
                                  "permissions": {"currentValue": preset},
                                  "sessionStats": {"turns": 0, "steps": 0},
                                  "contextPressure": {"contextWindow": 1000,
                                                      "pressureTokens": 100}}}}
        self.events[sid] = []
        return sid

    def emit(self, sid: str, etype: str, data: dict) -> dict:
        self.clock += 1000
        event = {"seq": len(self.events[sid]), "time": self.clock, "type": etype,
                 "data": data}
        self.events[sid].append(event)
        return event

    def tool(self, sid: str, turn: int, name: str, args: dict, result: str,
             error: bool = False) -> None:
        """One tool call and its result, the way dsh logs them."""
        cid = f"call_{len(self.events[sid])}"
        self.emit(sid, "tool/call", {"turn": turn, "step": 1, "callId": cid, "name": name,
                                     "arguments": json.dumps(args)})
        self.emit(sid, "tool/result", {"turn": turn, "step": 1, "message": {
            "source": {"kind": "tool", "callId": cid},
            "content": [{"type": "tool-result", "toolCallId": cid, "isError": error,
                         "content": [{"type": "text", "text": result}]}]}})

    def end_turn(self, sid: str, turn: int, kind: str = "completed") -> None:
        self.emit(sid, "turn/end", {"turn": turn, "reason": {"kind": kind}})

    def turn(self, sid: str, prompt: str, reply: str, kind: str = "completed") -> None:
        self.emit(sid, "user/message", {"content": [{"type": "text", "text": prompt}],
                                        "source": {"kind": "user"}})
        self.emit(sid, "user/message", {"content": [{"type": "text", "text": "runtime ctx"}],
                                        "source": {"kind": "plugin"}})
        self.emit(sid, "assistant/chunk", {"delta": reply[:3]})
        self.emit(sid, "assistant/message", {"message": {"role": "assistant",
                                                         "content": [{"type": "text",
                                                                      "text": reply}]}})
        self.emit(sid, "turn/end", {"reason": {"kind": kind}})

    def post(self, path: str, body: dict, label: str, timeout: float = 30.0) -> dict:
        self.posts.append((path, body))
        return {"accepted": True}

    def stream(self, path: str, settle: float = 1.5, max_seconds: float = 8.0) -> list[dict]:
        return list(self.frames)

    def rpc(self, method: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        if method in self.failing:
            raise RpcError(f"{method}: {self.failing[method]}")
        payload = payload or {}
        if method == "session.list":
            return {"items": list(self.sessions.values())}
        sid = payload.get("sessionId")
        if method == "session.history":
            events = self.events[sid]
            before = payload.get("beforeSeq")
            pool = [e for e in events if before is None or e["seq"] < before]
            page = pool[-payload.get("maxMessages", 40):]
            value = {"events": [{"event": e} for e in page],
                     "hasMore": bool(page) and page[0]["seq"] > 0}
            if before is None:
                value["projections"] = {"asOfSeq": events[-1]["seq"] if events else -1}
            return value
        if method == "session.prompt":
            self.prompts.append((sid, payload["content"][0]["text"]))
            self.emit(sid, "user/message", {"content": payload["content"],
                                            "source": {"kind": "user"}})
            self.sessions[sid]["running"] = True
            return {}
        if method == "session.create":
            new = self.add(payload.get("sessionId") or f"session-new{len(self.sessions)}")
            return {"sessionId": new}
        if method == "session.models":
            return {"current": {"provider": "deepseek-official", "model": "deepseek-flash",
                                "reasoningEffort": "high"}}
        if method == "session.rename":
            self.sessions[sid]["projections"]["values"]["title"] = payload["title"]
            return {"title": payload["title"], "seq": self.emit(sid, "session/title", {})["seq"]}
        raise AssertionError(f"fake has no {method}")
