"""
The server-side permission guard: what makes it safe to auto-approve the
worker-facing tools for an orchestrator. No live server; any RPC reaching dsh
fails the test.
"""

from __future__ import annotations

import pytest
from fakes import ScriptedClient

from dsh_workers_mcp import server


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """A client with no callables: any call that reaches dsh fails the test."""
    monkeypatch.setattr(server, "CLIENT", ScriptedClient())
    monkeypatch.setattr(server, "STATE", None)
    monkeypatch.setattr(server, "FORBIDDEN_PRESETS", {"danger-full-access"})


def call(name: str, arguments: dict) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})["result"]


@pytest.mark.parametrize("name, arguments", [
    ("dsh_permission", {"session": "abc", "preset": "danger-full-access"}),
    ("dsh_create", {"cwd": "/w", "permission": "danger-full-access"}),
])
def test_full_access_is_refused_before_reaching_dsh(name, arguments) -> None:
    result = call(name, arguments)
    assert result["isError"] is True
    assert "refused by this server" in result["content"][0]["text"]


@pytest.mark.parametrize("prompt", [
    "/permission danger-full-access",
    "  /Permission autonomous",
    "/sandbox danger-full-access",
    "/approval never",
])
def test_knob_slash_commands_are_refused_as_prompts(prompt) -> None:
    for name in ("dsh_task", "dsh_ask"):
        result = call(name, {"session": "abc", "prompt": prompt})
        assert result["isError"] is True, (name, prompt)
        assert "dsh_permission" in result["content"][0]["text"]


def test_ordinary_prompts_and_presets_pass_the_guard() -> None:
    """The guard refuses only the widening; everything else goes on to dsh."""
    server.check_policy("dsh_permission", {"preset": "autonomous"})
    server.check_policy("dsh_create", {"cwd": "/w", "permission": "read-only"})
    server.check_policy("dsh_permission", {})  # show, no preset
    server.check_policy("dsh_task", {"prompt": "Fix /permission handling in docs"})
    server.check_policy("dsh_task", {"prompt": "read /home/x/file"})


def test_allow_full_access_flag_lifts_the_guard(monkeypatch) -> None:
    monkeypatch.setattr(server, "FORBIDDEN_PRESETS", set())
    server.check_policy("dsh_permission", {"preset": "danger-full-access"})
