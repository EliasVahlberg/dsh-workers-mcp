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
            return {"current": {"provider": "deepseek-official", "model": "deepseek-flash",
                                "reasoningEffort": "high"}}
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


def test_retiring_a_worker_below_the_compaction_trigger_warns(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    call("dsh_roster", {"action": "adopt", "session": "aaaa1111", "role": "implementer"})
    dsh.sessions[sid]["projections"]["values"]["contextPressure"]["pressureTokens"] = 790
    result = json.loads(text(call("dsh_roster", {"action": "set", "session": "aaaa1111",
                                                 "retired": True})))
    assert "retired at 79.0% context" in result["warning"]
    assert "not a reason to retire" in result["warning"]
    assert state.roster()["workers"][sid]["notes"]["retired"] is True  # a reminder, not a block


def test_retiring_a_worker_near_the_window_does_not_warn(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    call("dsh_roster", {"action": "adopt", "session": "aaaa1111", "role": "implementer"})
    dsh.sessions[sid]["projections"]["values"]["contextPressure"]["pressureTokens"] = 900
    result = json.loads(text(call("dsh_roster", {"action": "set", "session": "aaaa1111",
                                                 "retired": True})))
    assert "warning" not in result


def test_un_retiring_or_setting_a_role_does_not_warn(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    call("dsh_roster", {"action": "adopt", "session": "aaaa1111", "role": "implementer"})
    result = json.loads(text(call("dsh_roster", {"action": "set", "session": "aaaa1111",
                                                 "role": "reviewer"})))
    assert "warning" not in result


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
    call("dsh_tasks", {"action": "update", "id": tid, "status": "abandoned", "note": "superseded"})
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


def test_task_writes_return_a_short_acknowledgement_and_show_the_full_record(dsh, state) -> None:
    added = text(call("dsh_tasks", {"action": "add", "title": "build the bench",
                                    "acceptance": "x" * 3000, "branch": "wt/bench"}))
    tid = json.loads(added)["id"]
    for note in range(30):
        call("dsh_tasks", {"action": "update", "id": tid, "note": f"round {note} " + "y" * 200})
    updated = text(call("dsh_tasks", {"action": "update", "id": tid, "status": "returned",
                                      "note": "done"}))
    assert len(added) < 500 and len(updated) < 500
    ack = json.loads(updated)
    assert ack["status"] == "returned" and ack["notes"] == 31 and ack["branch"] == "wt/bench"
    assert "history" not in ack and "acceptance" not in ack
    shown = json.loads(text(call("dsh_tasks", {"action": "show", "id": tid})))
    assert len(shown["notes"]) == 31 and shown["history"][-1]["status"] == "returned"


def test_roster_writes_return_a_short_acknowledgement(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    call("dsh_roster", {"action": "adopt", "session": "aaaa1111", "role": "reviewer"})
    for n in range(20):
        noted = text(call("dsh_roster", {"action": "note", "session": "aaaa1111",
                                         "text": f"note {n} " + "z" * 300}))
    assert len(noted) < 600
    ack = json.loads(noted)
    assert ack["short"] == "aaaa1111" and ack["role"] == "reviewer" and ack["notes"] == 20
    assert ack["lastNote"].startswith("note 19") and "auto" not in ack
    full = json.loads(text(call("dsh_roster", {"action": "show", "session": "aaaa1111"})))
    assert len(full["worker"]["notes"]["log"]) == 20


def test_closing_a_task_needs_a_note(dsh, state) -> None:
    tid = _task()
    for status in ("merged", "rejected", "abandoned", "done"):
        refused = call("dsh_tasks", {"action": "update", "id": tid, "status": status})
        assert refused["isError"] is True and "needs a note" in text(refused)
        blank = call("dsh_tasks", {"action": "update", "id": tid, "status": status, "note": "  "})
        assert blank["isError"] is True
    assert state.tasks()["tasks"][tid]["status"] == "open"           # nothing changed
    # A note that is not the closing note does not count, and open states need none.
    assert call("dsh_tasks", {"action": "update", "id": tid, "status": "in-review"})["isError"] is False
    ok = call("dsh_tasks", {"action": "update", "id": tid, "status": "merged", "note": "abc1234"})
    assert ok["isError"] is False and state.tasks()["tasks"][tid]["status"] == "merged"


def test_task_updates_are_validated(dsh, state) -> None:
    tid = _task()
    assert call("dsh_tasks", {"action": "update", "id": tid, "status": "finished"})["isError"]
    assert call("dsh_tasks", {"action": "add", "title": "x", "depends_on": ["T-404"]})["isError"]
    assert call("dsh_tasks", {"action": "add", "title": "  "})["isError"]
    updated = json.loads(text(call("dsh_tasks", {"action": "update", "id": tid,
                                                 "status": "in-review", "note": "tests pass"})))
    assert updated["status"] == "in-review" and updated["notes"] == 1
    shown = json.loads(text(call("dsh_tasks", {"action": "show", "id": tid})))
    assert shown["notes"][0]["text"] == "tests pass"
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


def test_add_can_claim_a_named_id_and_done_closes_a_decision(dsh, state) -> None:
    """Friction 27 Sep: batched adds swapped T-004/T-005; a decision task had no fitting status."""
    named = json.loads(text(call("dsh_tasks", {"action": "add", "id": "T-005",
                                               "title": "arbor 1x1"})))
    assert named["id"] == "T-005"
    auto = json.loads(text(call("dsh_tasks", {"action": "add", "title": "next"})))
    assert auto["id"] == "T-006"  # numbering continues past the claimed id
    assert call("dsh_tasks", {"action": "add", "id": "T-005", "title": "dup"})["isError"]
    assert call("dsh_tasks", {"action": "add", "id": "7", "title": "bad"})["isError"]

    call("dsh_tasks", {"action": "update", "id": "T-005", "status": "done",
                       "note": "decided: 1x1"})
    assert json.loads(text(call("dsh_tasks", {})))["tasks"][0]["id"] == "T-006"  # done is closed
    dsh.add("session-aaaa1111")
    result = call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": "T-005"})
    assert result["isError"] is True and "reopen" in text(result)


# ---------------------------------------------------------------- the digest

def _repo(tmp_path) -> Path:
    import subprocess
    repo = tmp_path / "wt"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                 ("config", "user.name", "t"), ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "first"], check=True,
                   capture_output=True)
    return repo


def test_digest_reads_the_mirror_and_uses_the_task(dsh, state, tmp_path) -> None:
    sid = dsh.add("session-aaaa1111")
    repo = _repo(tmp_path)
    tid = _task(acceptance="`make qa` passes", worktree=str(repo))
    call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": tid})
    assert state.tasks()["tasks"][tid]["dispatches"][0]["gitBefore"]["dirty"] == []

    dsh.tool(sid, 1, "edit", {"file_path": f"{repo}/a.txt"}, "updated")
    dsh.tool(sid, 1, "bash", {"command": f"cd {repo} && make qa 2>&1 | tail -2"}, "all green\n")
    dsh.end_turn(sid, 1)
    (repo / "a.txt").write_text("one\ntwo\n")        # the edit the worker made
    (repo / "b.txt").write_text("new\n")             # a file made by some command

    digest = json.loads(text(call("dsh_digest", {"session": "aaaa1111"})))
    assert digest["task"] == tid and digest["edited"] == ["a.txt"]
    assert digest["checks"][0]["via"] == "acceptance" and digest["checks"][0]["ok"] is True
    assert digest["verification"]["checkedAfterLastEdit"] is True
    assert digest["git"]["files"] == ["a.txt", "b.txt"] and digest["git"]["insertions"] == 1


def test_digest_needs_a_managed_session_and_a_known_task(dsh, state) -> None:
    dsh.add("session-aaaa1111")
    result = call("dsh_digest", {"session": "aaaa1111"})
    assert result["isError"] is True and "adopt" in text(result)
    state.adopt("session-aaaa1111", how="test")
    result = call("dsh_digest", {"session": "aaaa1111", "task_id": "T-404"})
    assert result["isError"] is True and "no task" in text(result)
    assert "no turns" in json.loads(text(call("dsh_digest", {"session": "aaaa1111"})))["note"]


def test_project_check_patterns_come_from_checks_txt(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    state.root.mkdir(parents=True, exist_ok=True)
    (state.root / "checks.txt").write_text("# this project's checks\n^\\./run_scenarios\\b\n([\n")
    dsh.tool(sid, 1, "bash", {"command": "./run_scenarios --all"}, "42 scenarios ok")
    dsh.end_turn(sid, 1)
    digest = json.loads(text(call("dsh_digest", {"session": "aaaa1111"})))
    assert digest["checks"][0]["via"] == "pattern" and digest["checks"][0]["tail"] == "42 scenarios ok"
    assert digest["badPatterns"] == ["(["]


def test_a_wait_carries_the_digest_only_when_asked_and_only_after_a_turn(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    dsh.tool(sid, 1, "edit", {"file_path": "/w/a.py"}, "ok")
    dsh.end_turn(sid, 1)
    done = json.dumps({"completed": True, "sessionId": sid})
    assert st.after_call(state, "dsh_wait", {"session": sid}, done) is None
    asked = st.after_call(state, "dsh_wait", {"session": sid, "digest": True}, done)
    assert asked["completed"] is True and asked["digest"]["edited"] == ["/w/a.py"]
    blocked = json.dumps({"completed": False, "blocked": True, "sessionId": sid, "pending": []})
    assert st.after_call(state, "dsh_wait", {"session": sid, "digest": True}, blocked) is None


# ------------------------------------------------------------ compaction watch

def _hist(*points) -> dict:
    """(usedPct, summaries) per turn end, then the summary count now."""
    *rows, now = points
    return {"summaries": now, "history": [{"seq": i, "usedPct": p, "summaries": s}
                                          for i, (p, s) in enumerate(rows)]}


@pytest.mark.parametrize("comp, stalled", [
    (_hist(0), False),                                            # nothing observed
    (_hist((79, 0), 0), False),                                   # healthy, near the trigger
    (_hist((82, 0), 0), False),                                   # one turn end a little over
    (_hist((85, 0), 0), False),                                   # the threshold is "above"
    (_hist((90, 0), 0), True),                                    # stalled at 90%
    (_hist((90, 0), 1), False),                                   # ...unless a summary landed since
    (_hist((81, 0), (83, 0), (84, 0), 0), True),                  # climbing, no summaries
    (_hist((81, 0), (83, 1), (84, 1), 1), False),                 # a summary between: it works
    (_hist((81, 0), (83, 0), 0), False),                          # only two turn ends
    (_hist((50, 0), (60, 0), (70, 0), 0), False),                 # a worker filling normally
    (_hist((82, 0), (81, 0), (84, 0), 0), False),                 # not monotonic
])
def test_compaction_stalled_judgment(comp, stalled) -> None:
    assert bool(st.compaction_stalled(comp)) is stalled


def _finish(dsh, state, sid, pct, summary=False, running=False) -> None:
    dsh.sessions[sid]["projections"]["values"]["contextPressure"]["pressureTokens"] = pct * 10
    dsh.sessions[sid]["running"] = running
    if summary:
        dsh.emit(sid, "compaction/summary", {})
    dsh.turn(sid, "work", "done")
    state.observe(sid)


def test_a_worker_near_the_trigger_that_compacts_is_healthy(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    for pct, summary in ((70, False), (81, False), (83, True), (82, False), (45, True), (79, False)):
        _finish(dsh, state, sid, pct, summary)
        assert "stalled" not in state.session_state(sid)["compaction"]
    assert state.session_state(sid)["compaction"]["summaries"] == 2


def test_a_stall_is_flagged_on_every_surface_and_clears_when_a_summary_lands(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test", role="implementer")
    _finish(dsh, state, sid, 90)
    reason = state.session_state(sid)["compaction"]["stalled"]
    assert "90%" in reason and "heuristic" in reason
    assert state.roster()["workers"][sid]["auto"]["compactionStalled"] == reason

    shown = json.loads(text(call("dsh_roster", {})))["workers"][0]
    assert shown["compactionStalled"] == reason
    status = st.after_call(state, "dsh_status", {"session": sid}, json.dumps({"sessionId": sid}))
    assert status["compactionStalled"] == reason
    listed = st.after_call(state, "dsh_list", {}, json.dumps([{"sessionId": sid}]))
    assert listed[0]["roster"]["compactionStalled"] == reason
    from dsh_workers_mcp import brief
    assert any("compaction looks stalled" in line
               for line in brief.build(state)["needsAction"])

    _finish(dsh, state, sid, 40, summary=True)      # it compacted after all
    assert "stalled" not in state.session_state(sid)["compaction"]
    assert st.after_call(state, "dsh_status", {"session": sid}, json.dumps({"sessionId": sid})) is None


def test_a_climb_with_no_summaries_is_flagged_after_three_turn_ends(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    for pct in (81, 83):
        _finish(dsh, state, sid, pct)
        assert "stalled" not in state.session_state(sid)["compaction"]
    _finish(dsh, state, sid, 84)
    assert "81% -> 83% -> 84%" in state.session_state(sid)["compaction"]["stalled"]


def test_a_level_read_mid_turn_is_not_a_turn_end(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    state.adopt(sid, how="test")
    _finish(dsh, state, sid, 95, running=True)       # a turn end seen while running again
    assert "history" not in state.session_state(sid)["compaction"]
    _finish(dsh, state, sid, 95, running=False)
    assert state.session_state(sid)["compaction"]["history"][-1]["usedPct"] == 95.0


def test_summaries_already_mirrored_are_counted_once_then_incrementally(dsh, state) -> None:
    sid = dsh.add("session-aaaa1111")
    dsh.emit(sid, "compaction/summary", {})
    dsh.emit(sid, "compaction/prune", {})
    dsh.emit(sid, "compaction/start", {})
    dsh.emit(sid, "compaction/summary", {})
    state.adopt(sid, how="test")
    state.observe(sid)
    assert state.session_state(sid)["compaction"]["summaries"] == 2     # start/prune not counted
    _finish(dsh, state, sid, 30, summary=True)
    assert state.session_state(sid)["compaction"]["summaries"] == 3


# ------------------------------------------------------- usage per dispatch

def _usage(dsh, sid, uncached, cache, output, steps, llm_ms, pressure=100) -> None:
    values = dsh.sessions[sid]["projections"]["values"]
    values["tokenUsage"] = {"uncachedInputTokens": uncached, "cacheReadTokens": cache,
                            "outputTokens": output}
    values["sessionStats"] = {"turns": 1, "steps": steps, "llmMs": llm_ms, "toolMs": 0}
    values["contextPressure"]["pressureTokens"] = pressure


def test_a_dispatch_records_the_worker_and_the_turn_end_records_what_it_used(
        dsh, state, monkeypatch) -> None:
    sid = dsh.add("session-aaaa1111", preset="supervised")
    tid = _task()
    monkeypatch.setattr(st, "now_iso", lambda: st.ms_to_iso(dsh.clock))
    _usage(dsh, sid, uncached=1000, cache=9000, output=500, steps=10, llm_ms=60_000, pressure=100)
    call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": tid})
    dispatch = state.tasks()["tasks"][tid]["dispatches"][0]
    assert dispatch["model"] == "deepseek-official/deepseek-flash" and dispatch["effort"] == "high"
    assert dispatch["permission"] == "supervised"
    assert dispatch["usageBefore"]["output"] == 500 and "usage" not in dispatch

    dsh.clock += 180_000
    _usage(dsh, sid, uncached=1400, cache=29000, output=2500, steps=45, llm_ms=240_000,
           pressure=300)
    dsh.turn(sid, "work", "done")
    state.observe(sid)

    usage = state.tasks()["tasks"][tid]["dispatches"][0]["usage"]
    assert usage["uncachedInput"] == 400 and usage["cacheRead"] == 20000
    assert usage["output"] == 2000 and usage["steps"] == 35 and usage["llmMinutes"] == 3.0
    assert usage["contextPct"] == [10.0, 30.0]
    assert 3.0 <= usage["wallMinutes"] <= 3.5
    shown = json.loads(text(call("dsh_tasks", {"action": "show", "id": tid})))
    assert shown["usage"]["rounds"] == 1 and shown["usage"]["models"] == [
        "deepseek-official/deepseek-flash/high"]
    assert shown["usage"]["total"]["output"] == 2000
    line = next(t for t in brief_lines(state) if t.startswith(tid))
    assert "1 round(s)" in line and "out 2,000" in line and "35 steps" in line


def brief_lines(state) -> list[str]:
    from dsh_workers_mcp import brief
    return brief.build(state)["openTasks"]


def test_fields_dsh_does_not_provide_are_omitted_not_invented(dsh, state, monkeypatch) -> None:
    sid = dsh.add("session-aaaa1111")
    dsh.sessions[sid]["projections"]["values"].pop("sessionStats")   # dsh reports no stats
    tid = _task()

    def models_down(method, payload=None, base="", timeout=0):
        if method == "session.models":
            raise dshctl.RpcError("no models")
        return dsh.rpc(method, payload, base, timeout)
    monkeypatch.setattr(dshctl, "rpc", models_down)
    call("dsh_task", {"session": "aaaa1111", "prompt": "go", "task_id": tid})
    dispatch = state.tasks()["tasks"][tid]["dispatches"][0]
    assert "model" not in dispatch and "effort" not in dispatch
    assert set(dispatch["usageBefore"]) == {"contextPct"}            # no tokens, no stats

    dsh.turn(sid, "work", "done")
    state.observe(sid)
    usage = state.tasks()["tasks"][tid]["dispatches"][0]["usage"]
    assert "output" not in usage and "steps" not in usage and "llmMinutes" not in usage


def test_rounds_count_dispatches_and_the_report_groups_by_model(dsh, state) -> None:
    flash = dsh.add("session-aaaa1111")
    other = dsh.add("session-bbbb2222")
    dsh.sessions[other]["projections"]["values"]  # same fake models; label them apart below
    t1, t2, t3 = _task(), _task(), _task()
    for tid, sid in ((t1, flash), (t2, flash), (t3, other)):
        call("dsh_task", {"session": sid[8:], "prompt": "go", "task_id": tid})
        dsh.turn(sid, "work", "done")
        state.observe(sid)
    call("dsh_tasks", {"action": "update", "id": t1, "status": "rejected", "note": "weak asserts"})
    call("dsh_task", {"session": "aaaa1111", "prompt": "again", "task_id": t1})   # rework
    dsh.turn(flash, "again", "done")
    state.observe(flash)
    call("dsh_tasks", {"action": "update", "id": t3, "status": "merged", "note": "abc1234"})
    # label the third task's first dispatch as another model
    data = state.tasks()
    data["tasks"][t3]["dispatches"][0].update(model="other/strong", effort="max")
    state._save_tasks(data)

    report = json.loads(text(call("dsh_tasks", {"action": "report"})))
    assert "money" in report["note"]
    flash_group = report["models"]["deepseek-official/deepseek-flash/high"]
    assert flash_group["tasks"] == 2 and flash_group["rounds"] == 3
    assert flash_group["roundsPerTask"] == 1.5
    assert report["models"]["other/strong/max"]["byStatus"] == {"merged": 1}
    assert {r["id"]: r["rounds"] for r in report["tasks"]} == {t1: 2, t2: 1, t3: 1}


def test_usage_delta_ignores_resets_and_missing_totals() -> None:
    assert st.usage_delta(None, {"output": 5}) == {}
    assert st.usage_delta({"output": 10, "steps": 3}, {"output": 4, "steps": 9}) == {"steps": 6}
    assert st.usage_delta({"output": 1}, {"steps": 9}) == {}
