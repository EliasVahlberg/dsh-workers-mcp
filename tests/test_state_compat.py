"""
A state directory written by the version before the split of `state.py` loads in the
split version and can be written on top of. The fixture was made by that old code,
through the server, with a scripted worker (see the quick start test for the flow).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fakes import ScriptedClient

from dsh_workers_mcp import brief, state as st
from dsh_workers_mcp.state import reports

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "state-v1"
SESSION = "session-new0"


@pytest.fixture
def old_state(tmp_path) -> st.State:
    shutil.copytree(FIXTURE, tmp_path / "orch")
    return st.State(tmp_path / "orch", client=ScriptedClient())      # any call to dsh fails the test


def test_the_roster_and_the_tasks_load(old_state) -> None:
    worker = old_state.roster()["workers"][SESSION]
    assert worker["notes"]["role"] == "implementer: hello file"
    assert worker["auto"]["lastTask"] == "T-001" and worker["auto"]["model"].endswith("deepseek-flash")
    task = old_state.tasks()["tasks"]["T-001"]
    assert task["status"] == "in-review" and task["dispatches"][0]["turnEnd"]["kind"] == "completed"
    assert old_state.session_state(SESSION)["lastTurnEnd"]["kind"] == "completed"
    assert old_state.is_managed(SESSION) and not old_state.is_managed("session-other")


def test_the_digest_and_the_brief_read_it(old_state) -> None:
    digest = reports.digest_for(old_state, SESSION, observe=False)
    assert digest["task"] == "T-001" and digest["turns"] == [1, 1]
    assert digest["checks"][0]["cmd"] == "make test" and digest["checks"][0]["ok"] is True
    summary = brief.build(old_state)
    assert summary["openTasks"][0].startswith("T-001 [in-review")
    assert any("T-001" in need for need in summary["needsAction"])
    assert summary["workers"][0].split(" | ")[0].endswith("implementer: hello file")


def test_it_can_be_written_on_top(old_state) -> None:
    old_state.update_task("T-001", status="merged", note="abc1234")
    old_state.add_note(SESSION, "retire after this")
    added = old_state.add_task("Next task")
    assert added["id"] == "T-002"
    on_disk = json.loads((old_state.root / "tasks.json").read_text())
    assert on_disk["tasks"]["T-001"]["status"] == "merged" and set(on_disk["tasks"]) == {"T-001", "T-002"}
    assert old_state.roster()["workers"][SESSION]["notes"]["log"][-1]["text"] == "retire after this"
    assert st.task_usage(on_disk["tasks"]["T-001"])["rounds"] == 1
