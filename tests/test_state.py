"""
State-layer tests against an in-memory fake of the dsh RPC surface. No live
server. The fake models the parts the state layer depends on: session.list
summaries, backwards-only history paging (beforeSeq), prompts appending real
user events, and renames.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsh_workers_mcp import dshctl, server, state as st


class FakeDsh:
    def __init__(self) -> None:
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

    def rpc(self, method: str, payload: dict | None = None, base: str = "", timeout=0):
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
            return {"current": {"provider": "deepseek-official", "model": "deepseek-flash"}}
        if method == "session.rename":
            self.sessions[sid]["projections"]["values"]["title"] = payload["title"]
            return {"title": payload["title"], "seq": self.emit(sid, "session/title", {})["seq"]}
        raise AssertionError(f"fake has no {method}")


@pytest.fixture
def dsh(monkeypatch) -> FakeDsh:
    fake = FakeDsh()
    monkeypatch.setattr(dshctl, "rpc", fake.rpc)
    return fake


@pytest.fixture
def state(tmp_path, monkeypatch) -> st.State:
    s = st.State(tmp_path / "orch")
    monkeypatch.setattr(server, "STATE", s)
    return s


def call(name: str, arguments: dict) -> dict:
    return server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": name, "arguments": arguments}})["result"]


def text(result: dict) -> str:
    return result["content"][0]["text"]


# ------------------------------------------------------------ state dir

def test_state_dir_is_explicit_or_cwd_relative(tmp_path) -> None:
    assert st.resolve_state_dir(str(tmp_path / "x")) == (tmp_path / "x").resolve()
    assert st.resolve_state_dir(None, cwd=str(tmp_path)) == tmp_path.resolve() / ".orchestrator"


def test_state_dir_is_created_lazily(tmp_path, monkeypatch) -> None:
    """Listing tools, or merely starting, must not litter a directory."""
    s = st.State(tmp_path / "orch")
    monkeypatch.setattr(server, "STATE", s)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert not s.root.exists()


def test_state_tools_hidden_without_a_state_dir(monkeypatch) -> None:
    monkeypatch.setattr(server, "STATE", None)
    names = {t["name"] for t in server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]}
    assert "dsh_roster" not in names and "dsh_list" in names
    assert call("dsh_roster", {})["isError"] is True


# ------------------------------------------------------------- call log

def test_every_call_is_logged_including_failures(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    call("dsh_list", {})
    call("dsh_status", {"session": "nomatch"})
    lines = [json.loads(line) for f in (state.root / "logs").glob("*.jsonl")
             for line in f.read_text().splitlines()]
    assert [(r["tool"], r["ok"]) for r in lines] == [("dsh_list", True), ("dsh_status", False)]
    assert "no session matching" in lines[1]["result"]
    assert all("durationMs" in r and "at" in r for r in lines)


# -------------------------------------------------------- history mirror

def test_history_mirror_is_incremental_and_drops_chunks(dsh, state, monkeypatch) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    dsh.turn(sid, "p1", "r1")
    monkeypatch.setattr(st, "HISTORY_PAGE_MESSAGES", 2)  # force a multi-page walk
    first = state.sync_history(sid)
    dsh.turn(sid, "p2", "r2")
    second = state.sync_history(sid)
    third = state.sync_history(sid)

    lines = [json.loads(line) for line in
             (state.session_dir(sid) / "events.jsonl").read_text().splitlines()]
    seqs = [e["seq"] for e in lines]
    assert seqs == sorted(set(seqs)), "each event written exactly once, in order"
    assert not any(e["type"] == "assistant/chunk" for e in lines)
    assert len(first["new"]) == 4 and len(second["new"]) == 4 and third["new"] == []


def test_mirror_that_cannot_reach_its_cursor_writes_nothing(dsh, state, monkeypatch) -> None:
    """A partial walk would leave a silent gap between the cursor and the page."""
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    for i in range(5):
        dsh.turn(sid, f"p{i}", f"r{i}")
    monkeypatch.setattr(st, "HISTORY_PAGE_MESSAGES", 2)
    monkeypatch.setattr(st, "HISTORY_MAX_PAGES", 2)
    with pytest.raises(dshctl.RpcError, match="did not reach its cursor"):
        state.sync_history(sid)
    assert not (state.session_dir(sid) / "events.jsonl").exists()


# --------------------------------------------------------- session state

def test_state_json_tracks_real_prompts_not_injected_ones(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    dsh.turn(sid, "the real prompt", "the reply", kind="max-tokens")
    snap = state.observe(sid)
    assert snap["lastPrompt"]["text"] == "the real prompt"
    assert snap["lastReply"]["text"] == "the reply"
    assert snap["lastTurnEnd"]["kind"] == "max-tokens"
    assert snap["observedAt"] and snap["lastPrompt"]["at"]
    assert snap["context"]["usedPct"] == 10.0


def test_unmanaged_sessions_are_not_observed(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    call("dsh_status", {"session": "aaaa1111"})
    assert not state.is_managed(sid)
    assert not state.session_dir(sid).exists()


# ---------------------------------------------------------------- roster

def test_create_fills_auto_fields_without_being_asked(dsh, state) -> None:
    result = call("dsh_create", {"cwd": "/w"})
    assert result["isError"] is False
    sid = json.loads(text(result))["sessionId"]
    auto = state.roster()["workers"][sid]["auto"]
    assert auto["addedBy"] == "dsh_create" and auto["createdAt"]
    assert auto["model"] == "deepseek-official/deepseek-flash"
    assert auto["permission"] == "supervised"


def test_name_is_recorded_and_ui_rename_is_reported_as_drift(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    call("dsh_name", {"session": "aaaa1111", "title": "implementer"})
    dsh.sessions[sid]["projections"]["values"]["title"] = "renamed in the UI"
    rows = json.loads(text(call("dsh_list", {})))
    row = next(r for r in rows if r["sessionId"] == sid)
    assert any("differs from the name set here" in d for d in row["roster"]["drift"])


def test_deleted_session_is_reported_missing(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    del dsh.sessions[sid]
    rows = json.loads(text(call("dsh_list", {})))
    assert {"sessionId": sid, "roster": {"missing": True,
            "drift": ["session no longer exists in dsh"]}} in rows


def test_judgment_goes_through_the_tool_and_cannot_touch_auto(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    call("dsh_roster", {"action": "adopt", "session": "aaaa1111", "role": "reviewer"})
    call("dsh_roster", {"action": "note", "session": "aaaa1111", "text": "good at tests"})
    worker = state.roster()["workers"][sid]
    assert worker["notes"]["role"] == "reviewer"
    assert worker["notes"]["log"][0]["text"] == "good at tests"
    assert call("dsh_roster", {"action": "set", "session": "aaaa1111"})["isError"] is True
    call("dsh_roster", {"action": "set", "session": "aaaa1111", "retired": True})
    assert json.loads(text(call("dsh_roster", {})))["workers"] == []


# ----------------------------------------------------------------- tasks

def _task(title="build the bench", **kw) -> str:
    return json.loads(text(call("dsh_tasks", {"action": "add", "title": title, **kw})))["id"]


def test_bad_task_id_refuses_before_anything_is_sent(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    result = call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": "T-999"})
    assert result["isError"] is True and "no task" in text(result)
    assert dsh.prompts == []


def test_task_owned_by_another_worker_refuses(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    dsh.add("session-bbbb2222")
    tid = _task(owner="bbbb2222")
    result = call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": tid})
    assert result["isError"] is True and "owned by" in text(result)
    assert dsh.prompts == []


def test_task_lifecycle_dispatched_blocked_returned(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    tid = _task()
    assert call("dsh_task", {"session": "aaaa1111", "prompt": "go",
                             "task_id": tid})["isError"] is False
    task = state.tasks()["tasks"][tid]
    assert task["status"] == "dispatched" and task["owner"] == sid
    assert state.roster()["workers"][sid]["auto"]["currentTask"] == tid

    # a wait that found the worker blocked on an approval
    blocked_out = json.dumps({"completed": False, "blocked": True, "sessionId": sid,
                              "pending": [{"kind": "approval/requested", "rpcId": "r1",
                                           "toolName": "web_search"}]})
    st.after_call(state, "dsh_wait", {"session": sid}, blocked_out)
    assert state.tasks()["tasks"][tid]["status"] == "blocked"
    assert state.session_state(sid)["blocked"]["pending"][0]["toolName"] == "web_search"

    # a status poll cannot see pending requests, so it must not clear `blocked`
    st.after_call(state, "dsh_status", {"session": sid}, json.dumps({"sessionId": sid}))
    assert state.tasks()["tasks"][tid]["status"] == "blocked"

    st.after_call(state, "dsh_approve", {"session": sid}, json.dumps({"outcome": "allowed-once"}))
    assert state.tasks()["tasks"][tid]["status"] == "dispatched"
    assert "blocked" not in state.session_state(sid)

    dsh.emit(sid, "assistant/message", {"message": {"content": [{"type": "text",
                                                                 "text": "done"}]}})
    dsh.emit(sid, "turn/end", {"reason": {"kind": "completed"}})
    dsh.sessions[sid]["running"] = False
    st.after_call(state, "dsh_wait", {"session": sid}, json.dumps({"completed": True}))
    task = state.tasks()["tasks"][tid]
    assert task["status"] == "returned"
    assert task["dispatches"][-1]["turnEnd"]["kind"] == "completed"
    assert [h["status"] for h in task["history"]] == \
        ["open", "dispatched", "blocked", "dispatched", "returned"]
    auto = state.roster()["workers"][sid]["auto"]
    assert auto["currentTask"] is None and auto["lastTask"] == tid


def test_closed_tasks_are_left_alone(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    tid = _task()
    call("dsh_tasks", {"action": "update", "id": tid, "status": "abandoned"})
    result = call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": tid})
    assert result["isError"] is True and "reopen" in text(result)
    assert json.loads(text(call("dsh_tasks", {})))["tasks"] == []
    assert len(json.loads(text(call("dsh_tasks", {"include_closed": True})))["tasks"]) == 1


def test_rejected_task_goes_back_to_its_worker_by_dispatching_it(dsh, state) -> None:
    """METHOD: send a rejection back to the same worker. The dispatch is the reopen."""
    sid = dsh.add("session-aaaa1111")
    tid = _task()
    call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": tid})
    call("dsh_tasks", {"action": "update", "id": tid, "status": "rejected",
                       "note": "conservation asserts too weak"})
    result = call("dsh_task", {"session": "aaaa1111", "prompt": "rework", "task_id": tid})
    assert result["isError"] is False
    task = state.tasks()["tasks"][tid]
    assert task["status"] == "dispatched" and task["owner"] == sid
    assert [h["status"] for h in task["history"]][-3:] == ["rejected", "open", "dispatched"]
    assert task["history"][-2]["by"] == "server"
    assert len(task["dispatches"]) == 2


def test_task_updates_are_validated(dsh, state) -> None:
    tid = _task()
    assert call("dsh_tasks", {"action": "update", "id": tid, "status": "done"})["isError"]
    assert call("dsh_tasks", {"action": "add", "title": "x", "depends_on": ["T-404"]})["isError"]
    assert call("dsh_tasks", {"action": "add", "title": "  "})["isError"]
    updated = json.loads(text(call("dsh_tasks", {"action": "update", "id": tid,
                                                 "status": "in-review", "note": "tests pass"})))
    assert updated["status"] == "in-review" and updated["notes"][0]["text"] == "tests pass"
    assert _task("second") == "T-002"


# ------------------------------------------------- recording never lies

def test_a_state_failure_does_not_turn_success_into_error(dsh, state, monkeypatch) -> None:
    """Reporting a dispatched task as failed would make the orchestrator re-send it."""
    dsh.add("session-aaaa1111")

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(st, "after_call", broken)
    result = call("dsh_task", {"session": "aaaa1111", "prompt": "go"})
    assert result["isError"] is False
    assert "recording it" in text(result) and "disk full" in text(result)
    assert len(dsh.prompts) == 1


def test_state_files_are_valid_json_after_writes(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    _task()
    call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": "T-001"})
    for path in (state.roster_path, state.tasks_path):
        json.loads(Path(path).read_text())
    assert not list(state.root.glob("*.tmp")) and not list(state.root.glob(".*.tmp"))
