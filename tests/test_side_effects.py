"""
Two dsh side effects the orchestrator hit on its first hire, and the guards
against them: a model pin re-defaulting every later session, and `goal set`
starting an unattended work loop.
"""

from __future__ import annotations

import pytest

from dsh_workers_mcp import dshctl, server

SETTINGS = """\
llm-pi-ai:
  providers: {}
agent-default-model:
  # a comment dsh may or may not keep
  provider: deepseek-official
  model: deepseek-flash
other-key: 1
"""


@pytest.fixture
def dsh_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_HOME", str(tmp_path))
    (tmp_path / "settings.yaml").write_text(SETTINGS)
    return tmp_path


def fake_rpc(home, rewrite):
    calls = []

    def rpc(method, payload, base=None):
        calls.append(method)
        if method == "session.create":
            return {"sessionId": "session-new"}
        if method == "session.selectModel" and rewrite:
            # What dsh does: persist the pin as the global default.
            text = (home / "settings.yaml").read_text()
            text = text.replace("provider: deepseek-official", f"provider: {payload['provider']}")
            text = text.replace("model: deepseek-flash", f"model: {payload['model']}")
            (home / "settings.yaml").write_text(text + "late-edit: kept\n")
        return {}

    return rpc, calls


def create(monkeypatch, capsys, rpc) -> dict:
    import json
    monkeypatch.setattr(dshctl, "rpc", rpc)
    args = type("A", (), dict(cwd="/w", preset=None, session_id=None, provider="qwen-cloud",
                              model="qwen3.8-flash", permission=None, base=None))()
    assert dshctl.cmd_create(args) == 0
    return json.loads(capsys.readouterr().out)


def test_pin_restores_the_global_default(dsh_home, monkeypatch, capsys) -> None:
    rpc, calls = fake_rpc(dsh_home, rewrite=True)
    out = create(monkeypatch, capsys, rpc)
    assert out["selectedModel"] == "qwen-cloud/qwen3.8-flash"
    assert out["defaultModel"] == ("restored to deepseek-official/deepseek-flash (dsh had "
                                   "saved the pin as the default: qwen-cloud/qwen3.8-flash)")
    text = (dsh_home / "settings.yaml").read_text()
    assert dshctl.default_model_lines(text) == {
        "provider": "  provider: deepseek-official", "model": "  model: deepseek-flash"}
    assert "late-edit: kept" in text and "other-key: 1" in text  # the rest is untouched
    assert "qwen" not in text


def test_pin_that_did_not_rewrite_reports_unchanged(dsh_home, monkeypatch, capsys) -> None:
    rpc, _ = fake_rpc(dsh_home, rewrite=False)
    assert create(monkeypatch, capsys, rpc)["defaultModel"] == \
        "unchanged: deepseek-official/deepseek-flash"
    assert (dsh_home / "settings.yaml").read_text() == SETTINGS


def test_goal_set_needs_explicit_start(monkeypatch) -> None:
    monkeypatch.setattr(dshctl, "find_session", lambda s, b=None: {"sessionId": "session-x"})
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("must not reach dsh")))
    monkeypatch.setattr(server, "STATE", None)
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "dsh_goal", "arguments": {"session": "x", "action": "set", "objective": "o"}}})
    assert result["result"]["isError"] is True
    assert "autonomous goal loop" in result["result"]["content"][0]["text"]


def test_goal_set_with_start_reaches_dsh(monkeypatch) -> None:
    sent = []
    monkeypatch.setattr(dshctl, "find_session", lambda s, b=None: {"sessionId": "session-x"})
    monkeypatch.setattr(dshctl, "rpc", lambda m, p, base=None: sent.append((m, p)) or {"ok": 1})
    monkeypatch.setattr(server, "STATE", None)
    result = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "dsh_goal", "arguments": {"session": "x", "action": "set", "objective": "o",
                                          "start": True}}})
    assert result["result"]["isError"] is False
    assert sent == [("goal.create", {"sessionId": "session-x", "objective": "o"})]


def test_same_model_pin_names_the_reasoning_effort_dsh_added(dsh_home) -> None:
    """Measured live: pinning the default model itself still rewrites the block."""
    before = (dsh_home / "settings.yaml").read_text()
    (dsh_home / "settings.yaml").write_text(
        before.replace("  model: deepseek-flash\n", "  model: deepseek-flash\n  reasoningEffort: high\n"))
    wrote = dshctl.restore_default_model(before)
    assert wrote == "deepseek-official/deepseek-flash (reasoningEffort high)"
    assert (dsh_home / "settings.yaml").read_text() == before
