"""
No id, name or note that reaches the state directory can write outside it.

SECURITY.md lists a path escape through a crafted session id, task id or role name as
a vulnerability. A session id becomes a directory name, so `Store.session_dir` checks
it. Task ids and roles are values in JSON files and never become paths.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dsh_workers_mcp import server
from dsh_workers_mcp import state as st
from dsh_workers_mcp.transport import RpcError

HOSTILE = ["../evil", "..", ".", "a/b", "/etc/passwd", "", "x/../../y", "a\x00b", "-rf/..", ".hidden", "a b"]
REAL = ["session-4f27ceef-b9f6-450a-8258-7706836cfdec", "25616a9a-1509-4b0d-8ce0-cac3e667ae88",
        "session-new0", "a.b_c-d"]


def tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.mark.parametrize("session_id", HOSTILE)
def test_a_session_id_that_is_not_a_plain_name_is_refused(tmp_path, session_id) -> None:
    store = st.State(tmp_path / "orch")
    with pytest.raises(RpcError, match="refusing session id"):
        store.session_dir(session_id)


@pytest.mark.parametrize("session_id", REAL)
def test_the_ids_that_dsh_makes_are_accepted(tmp_path, session_id) -> None:
    store = st.State(tmp_path / "orch")
    assert store.session_dir(session_id) == tmp_path / "orch" / "history" / session_id


def test_a_hostile_session_id_from_dsh_writes_nothing_outside_the_state_dir(tmp_path, dsh) -> None:
    """If dsh itself returned such an id, the mirror must fail, not write to ../."""
    dsh.add("../evil")
    store = st.State(tmp_path / "orch", client=dsh)
    with pytest.raises(RpcError, match="refusing"):
        store.sync_history("../evil")
    assert not [p for p in tmp_path.rglob("*") if p.name == "evil"]


def test_a_task_id_that_is_not_a_task_number_is_refused(state) -> None:
    for task_id in ("../../outside", "T-1/../x", "t-001"):
        with pytest.raises(RpcError, match="must look like"):
            state.add_task("title", task_id=task_id)


def test_titles_roles_and_notes_with_path_characters_stay_inside_the_state_dir(state, dsh, tmp_path) -> None:
    """They are JSON values. No file is made from them."""
    dsh.add("session-aaaa1111")
    nasty = "../../outside"
    server.call_tool("dsh_tasks", {"action": "add", "title": nasty, "acceptance": nasty})
    server.call_tool("dsh_roster", {"action": "adopt", "session": "session-aaaa1111", "role": nasty})
    server.call_tool("dsh_roster", {"action": "note", "session": "session-aaaa1111", "text": nasty})
    assert not (tmp_path.parent / "outside").exists() and not (tmp_path / "outside").exists()
    assert not [p for p in tree(state.root) if "outside" in p]
    assert nasty in (state.root / "tasks.json").read_text()      # stored as data, not as a path
