"""
The contract between this package and the dsh API, in three parts that must agree:
`shape.REQUIRED` (what the code reads), `contracts/dsh-shapes.json` (what a real dsh
returned, as keys and types only) and `FakeDsh` (what the tests pretend dsh returns).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fakes import FakeDsh, ScriptedClient

from dsh_workers_mcp import brief, shape, state as st
from dsh_workers_mcp.transport import RpcError

RECORDED = json.loads((Path(__file__).resolve().parent.parent / "contracts" / "dsh-shapes.json").read_text())


# ---------------------------------------------------------------- the recording

def test_the_recording_names_the_dsh_it_came_from() -> None:
    assert RECORDED["dsh"].get("commit") and RECORDED["dsh"].get("package")


def test_the_recording_holds_keys_and_types_only() -> None:
    def leaves(node):
        if isinstance(node, dict):
            for item in node.values():
                yield from leaves(item)
        elif isinstance(node, list):
            for item in node:
                yield from leaves(item)
        else:
            yield node

    allowed = {"str", "int", "float", "bool", "null", "any"}
    for method, document in RECORDED["shapes"].items():
        for leaf in leaves(document):
            assert set(leaf.split("|")) <= allowed, (method, leaf)


@pytest.mark.parametrize("method", sorted(set(RECORDED["shapes"]) & set(shape.REQUIRED)))
def test_what_the_code_reads_is_in_the_real_response(method: str) -> None:
    assert shape.conforms(RECORDED["shapes"][method], shape.REQUIRED[method]) == []


def test_the_recording_covers_the_methods_that_have_no_side_effect() -> None:
    read_only = {"session.list", "session.history", "session.models"}
    assert read_only <= set(RECORDED["shapes"])


# --------------------------------------------------------------------- the fake

def test_the_fake_returns_what_the_code_reads() -> None:
    fake = FakeDsh()
    sid = fake.add("session-aaaa1111")
    fake.turn(sid, "go", "done")
    responses = {
        "session.list": fake.rpc("session.list"),
        "session.history": fake.rpc("session.history", {"sessionId": sid, "maxMessages": 10}),
        "session.models": fake.rpc("session.models", {"sessionId": sid}),
        "session.create": fake.rpc("session.create", {}),
        "session.rename": fake.rpc("session.rename", {"sessionId": sid, "title": "t"}),
    }
    for method, value in responses.items():
        assert shape.check(method, value) == [], method


# ------------------------------------------------------------ the check itself

def test_skeleton_keeps_keys_and_types_and_drops_content() -> None:
    value = {"items": [{"id": "secret", "n": 3, "x": None}, {"id": "other", "n": 4.5, "y": True}]}
    assert shape.skeleton(value) == {"items": [{"id": "str", "n": "float|int", "x": "null", "y": "bool"}]}
    assert "secret" not in json.dumps(shape.skeleton(value))
    assert shape.skeleton({"a": []}) == {"a": []}


def test_conforms_names_the_field_that_is_missing_or_has_another_type() -> None:
    required = {"items": [{"sessionId": "str", "running": "bool"}], "ok": "bool|null"}
    good = {"items": [{"sessionId": "str", "running": "bool", "extra": "int"}], "ok": "null"}
    assert shape.conforms(good, required) == []
    assert shape.conforms({"items": [], "ok": "bool"}, required) == []          # nothing to compare
    assert shape.conforms({"items": [{"sessionId": "str"}], "ok": "bool"}, required) == [
        "items[].running is missing"]
    assert shape.conforms({"items": [{"sessionId": "int", "running": "bool"}], "ok": "str"}, required) == [
        "items[].sessionId is int, expected str", "ok is str, expected bool|null"]
    assert shape.conforms({"items": {}, "ok": "bool"}, required) == ["items should be a list"]
    assert shape.conforms("str", {"a": "str"}) == ["value should be an object"]


def test_check_ignores_methods_it_has_no_requirement_for() -> None:
    assert shape.check("goal.create", {"anything": 1}) == []


# ------------------------------------------------------------ the transport boundary

def test_only_the_transport_module_opens_a_connection() -> None:
    src = Path(__file__).resolve().parent.parent / "src" / "dsh_workers_mcp"
    offenders = [path.name for path in sorted(src.glob("*.py")) if path.name != "transport.py"
                 and re.search(r"^\s*(import|from)\s+(urllib|socket|http|ssl)\b", path.read_text(), re.M)]
    assert offenders == []


# ----------------------------------------------------------- reporting the drift

def test_checked_client_records_drift_once_and_never_raises() -> None:
    inner = ScriptedClient(rpc=lambda method, payload: {"items": [{"sessionId": "s1"}]})
    client = shape.CheckedClient(inner)
    for _ in range(5):
        assert client.rpc("session.list") == {"items": [{"sessionId": "s1"}]}   # the value is untouched
    assert client.drift_lines() == [
        "session.list: items[].running is missing",
        "session.list: items[].projections is missing"]
    assert client.rpc("session.models") == {"items": [{"sessionId": "s1"}]}
    assert "session.models" in client.drift


def test_checked_client_stays_quiet_when_the_shape_fits() -> None:
    fake = FakeDsh()
    fake.add("session-aaaa1111")
    client = shape.CheckedClient(fake)
    client.rpc("session.list")
    assert client.drift == {} and client.drift_lines() == []
    assert client.base == fake.base


def test_a_failing_call_passes_through_unchanged() -> None:
    fake = FakeDsh()
    fake.failing["session.list"] = "down"
    with pytest.raises(RpcError, match="down"):
        shape.CheckedClient(fake).rpc("session.list")


def test_the_brief_tells_the_lead_to_report_drift(tmp_path) -> None:
    inner = ScriptedClient(rpc=lambda method, payload: {"items": []} if method == "session.list" else {},
                           stream=lambda path: [])
    client = shape.CheckedClient(inner)
    state = st.State(tmp_path / "orch", client=client)
    clean = brief.cmd_brief(state, {})
    assert "dshShape" not in clean
    inner._rpc = lambda method, payload: {"items": [{"sessionId": "s1"}]}
    out = brief.cmd_brief(state, {})
    assert out["dshShape"] and brief.SHAPE_DRIFT_NOTE in out["needsAction"]
    assert "dshShape" not in brief.cmd_brief(st.State(tmp_path / "other", client=inner), {"live": False})
