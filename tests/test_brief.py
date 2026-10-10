"""dsh_brief: the sweep and its to-do list, from fixture state."""

import datetime as dt
import json
import os
import stat
import subprocess

import pytest

from dsh_workers_mcp import brief, digest, dshctl, server, state as st

A, B, C, D = ("session-aaaa1111", "session-bbbb2222", "session-cccc3333", "session-dddd4444")


@pytest.fixture
def state(tmp_path, monkeypatch) -> st.State:
    s = st.State(tmp_path / "orch")
    monkeypatch.setattr(server, "STATE", s)
    return s


def worker(state, sid, role, *, running=False, task=None, missing=False, retired=False):
    state.adopt(sid, how="test", role=role)
    state._update_worker(sid, auto={"short": sid[8:16], "running": running,
                                    "currentTask": task, "missing": missing})
    if retired:
        state.set_judgment(sid, retired=True)


def task(state, title, status, owner=None, **fields) -> str:
    tid = state.add_task(title, owner=owner, **fields)["id"]
    if status != "open":
        state.update_task(tid, status=status)
    return tid


def later(days=0, hours=0) -> dt.datetime:
    return dt.datetime.now().astimezone() + dt.timedelta(days=days, hours=hours)


def test_needs_action_names_what_the_lead_must_do(state) -> None:
    worker(state, A, "implementer", running=True, task="T-001")
    worker(state, B, "reviewer")                         # idle, no task
    worker(state, C, "implementer", missing=True)        # gone from dsh
    worker(state, D, "old", retired=True)                # not listed at all
    ret = task(state, "returned one", "returned", A)
    blk = task(state, "blocked one", "blocked", A)
    rev = task(state, "half reviewed", "in-review", A)
    task(state, "in flight", "dispatched", A)
    task(state, "not started", "open")
    task(state, "finished", "merged", A)
    pending = [{"kind": "approval/requested", "rpcId": "r9", "sessionId": A,
                "toolName": "web_search"},
               {"kind": "question/requested", "rpcId": "r10", "sessionId": B}]

    out = brief.build(state, now=later(days=2), pending=pending, dsh="up")

    needs = out["needsAction"]
    assert f"verdict on {ret}: returned 2d ago" in needs
    assert f"answer for {blk}: blocked 2d" in needs
    assert any(rev in line and "in-review" in line for line in needs)
    assert "pending from aaaa1111: web_search (rpc_id r9)" in needs
    assert "pending from bbbb2222: question (rpc_id r10)" in needs
    assert "work for bbbb2222 (reviewer): idle, no task" in needs
    assert "cccc3333: session no longer exists in dsh" in needs
    assert not any("dddd4444" in line for line in needs + out["workers"])
    assert len(out["openTasks"]) == 5 and not any("finished" in t for t in out["openTasks"])
    assert out["workers"][0].startswith("aaaa1111 implementer | running | task T-001")


def test_a_quiet_roster_has_nothing_to_do(state) -> None:
    worker(state, A, "implementer", running=True, task="T-001")
    task(state, "in flight", "dispatched", A)
    assert brief.build(state, pending=[], dsh="up")["needsAction"] == []
    assert brief.build(st.State(state.root.parent / "empty"))["workers"] == []


def test_ten_open_tasks_stay_under_two_thousand_characters(state) -> None:
    for sid, role in ((A, "impl one"), (B, "impl two"), (C, "reviewer")):
        worker(state, sid, role, running=True, task="T-001")
    for i in range(10):
        task(state, f"A task with a reasonably long title number {i} for the sweep",
             ["returned", "blocked", "dispatched", "open", "in-review"][i % 5],
             [A, B, C][i % 3])
    out = brief.build(state, now=later(days=1), dsh="up")
    assert len(json.dumps(out, indent=2)) < 2000


def _git(repo, *args) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_tasks_being_worked_on_show_the_worktree_size(state, tmp_path) -> None:
    repo = tmp_path / "wt"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@e.x"),
                 ("config", "user.name", "t"), ("config", "commit.gpgsign", "false")):
        _git(repo, *args)
    (repo / "a.txt").write_text("1\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    worker(state, A, "implementer", running=True, task="T-001")
    tid = task(state, "sized", "open", A, worktree=str(repo))
    state.record_dispatch(tid, A, "go", "queue", 0)
    (repo / "a.txt").write_text("1\n2\n3\n")
    (repo / "b.txt").write_text("new\n")
    line = brief.build(state, dsh="up")["openTasks"][0]
    assert line.startswith(f"{tid} [dispatched ") and "2 file(s)" in line
    assert "+2 -0" in line and "2 uncommitted" in line and "commit(s)" not in line


def _hook(state, body: str, mode: int = 0o755) -> None:
    path = state.root / brief.HOOK_NAME
    state.root.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def test_the_project_hook_adds_lines_and_is_optional(state, monkeypatch) -> None:
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: {"items": []})
    monkeypatch.setattr(dshctl, "pending_requests", lambda *a, **k: [])
    assert "project" not in brief.cmd_brief(state, {})                   # no hook: fine
    _hook(state, "#!/bin/sh\necho \"orch=$ORCHESTRATOR_DIR\"\npwd\n")
    lines = brief.cmd_brief(state, {})["project"]
    assert lines == [f"orch={state.root}", str(state.root.parent)]
    _hook(state, "#!/bin/sh\necho x\n", mode=0o644)                      # not executable
    assert "project" not in brief.cmd_brief(state, {})


def test_a_failing_or_chatty_hook_is_contained(state) -> None:
    _hook(state, "#!/bin/sh\nfor i in $(seq 1 50); do echo line$i; done\nexit 3\n")
    lines = brief.run_hook(state)
    assert len(lines) == brief.HOOK_MAX_LINES + 1 and lines[-1] == "+31 more"
    _hook(state, "#!/bin/sh\necho ok\nexit 3\n")
    assert brief.run_hook(state) == ["ok", f"{brief.HOOK_NAME} exited 3"]
    _hook(state, "#!/nonexistent/interpreter\n")
    assert "failed" in brief.run_hook(state)[0]


def _summary(sid, preset="supervised", running=False):
    return {"sessionId": sid, "running": running, "cwd": "/w", "agentPreset": "standard",
            "projections": {"values": {"title": "t", "permissions": {"currentValue": preset}}}}


def test_live_brief_refreshes_reads_pending_and_reports_drift(state, monkeypatch) -> None:
    worker(state, A, "implementer", task="T-001")
    worker(state, B, "reviewer")
    state._update_worker(A, auto={"permissionSet": "read-only"})   # we set it; dsh differs
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: {"items": [_summary(A)]})  # B is gone
    monkeypatch.setattr(st.State, "observe", lambda self, sid, summary=None, **k: {})
    monkeypatch.setattr(dshctl, "pending_requests", lambda *a, **k: [
        {"kind": "approval/requested", "rpcId": "r1", "sessionId": A, "toolName": "bash"}])

    out = brief.cmd_brief(state, {})
    assert out["dsh"] == "up"
    needs = out["needsAction"]
    assert "pending from aaaa1111: bash (rpc_id r1)" in needs
    assert "bbbb2222: session no longer exists in dsh" in needs
    assert any("aaaa1111: permission" in line for line in needs)


def test_with_dsh_down_the_brief_answers_from_state_and_says_so(state, monkeypatch) -> None:
    worker(state, A, "implementer", running=True, task="T-001")

    def down(*args, **kwargs):
        raise dshctl.RpcError("connection refused")
    monkeypatch.setattr(dshctl, "rpc", down)
    out = brief.cmd_brief(state, {})
    assert out["dsh"].startswith("unreachable") and out["workers"]


def test_unreadable_pending_is_flagged_not_hidden(state, monkeypatch) -> None:
    worker(state, A, "implementer", running=True, task="T-001")
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: {"items": [_summary(A)]})
    monkeypatch.setattr(st.State, "observe", lambda self, sid, summary=None, **k: {})

    def broken(*args, **kwargs):
        raise dshctl.RpcError("no stream")
    monkeypatch.setattr(dshctl, "pending_requests", broken)
    assert "could not read dsh_pending: call it yourself" in brief.cmd_brief(state, {})["needsAction"]


def test_live_false_touches_nothing(state, monkeypatch) -> None:
    worker(state, A, "implementer", running=True, task="T-001")

    def boom(*args, **kwargs):
        raise AssertionError("must not call dsh")
    monkeypatch.setattr(dshctl, "rpc", boom)
    monkeypatch.setattr(dshctl, "pending_requests", boom)
    out = brief.cmd_brief(state, {"live": False})
    assert out["dsh"].startswith("not checked") and out["needsAction"] == []


def test_the_tool_is_served_through_the_server(state, monkeypatch) -> None:
    worker(state, A, "implementer", running=True, task="T-001")
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "dsh_brief", "arguments": {"live": False}}})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["workers"][0].startswith("aaaa1111 implementer")


@pytest.mark.parametrize("delta, expected", [
    (dt.timedelta(seconds=30), "0m"), (dt.timedelta(minutes=5), "5m"),
    (dt.timedelta(hours=3), "3h"), (dt.timedelta(days=2, hours=5), "2d"),
])
def test_age(delta, expected) -> None:
    now = dt.datetime.now().astimezone()
    assert brief.age((now - delta).isoformat(), now) == expected
    assert brief.age(None, now) == "?" and brief.age("garbage", now) == "?"


def test_long_free_text_roles_are_cut_to_their_first_clause() -> None:
    long_role = "implementer \u2014 billing API (invoices, tax rules, retries, audit trail)"
    assert brief._role({"role": long_role}) == "implementer"
    assert brief._role({"role": "x" * 100}) == "x" * 39 + "\u2026"
    assert brief._role({}) == "(no role)"
