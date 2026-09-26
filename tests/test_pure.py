"""
Regression tests for the pure functions -- no live dsh server required.

Every test here corresponds to a bug that actually happened or to an invariant
whose violation would silently corrupt what an orchestrator believes about a
worker. That is the selection criterion: this file is not for coverage, it is
for the places where being wrong is quiet.
"""

from __future__ import annotations

import io
import json
import time

import pytest

from dsh_workers_mcp import dshctl


# --------------------------------------------------------- reasoning leakage
#
# The original bug: an assistant/message's content holds both a reasoning block
# and a text block, and BOTH carry a `text` field. A naive "any block with text"
# extractor concatenates private thinking into what reads as the answer. It was
# misdiagnosed as a vendor defect before being traced here.

def _message(blocks: list[dict], etype: str = "assistant/message") -> dict:
    return {"type": etype, "seq": 1, "data": {"message": {"content": blocks}}}


def test_text_of_excludes_reasoning_blocks() -> None:
    event = _message([
        {"type": "reasoning", "text": "PRIVATE deliberation"},
        {"type": "text", "text": "the visible answer"},
    ])
    assert dshctl.text_of(event) == "the visible answer"
    assert "PRIVATE" not in dshctl.text_of(event)


def test_text_of_can_opt_into_reasoning() -> None:
    event = _message([
        {"type": "reasoning", "text": "thinking"},
        {"type": "text", "text": "answer"},
    ])
    assert dshctl.text_of(event, include_reasoning=True) == "thinking\nanswer"


def test_reasoning_of_returns_only_reasoning() -> None:
    event = _message([
        {"type": "thinking", "text": "step one"},
        {"type": "text", "text": "answer"},
        {"type": "reasoning", "text": "step two"},
    ])
    assert dshctl.reasoning_of(event) == "step one\nstep two"


def test_reasoning_and_answer_partition_the_content() -> None:
    """No block may be dropped by both accessors, or silently counted by both."""
    event = _message([
        {"type": "reasoning", "text": "R"},
        {"type": "text", "text": "A"},
    ])
    assert dshctl.text_of(event) == "A"
    assert dshctl.reasoning_of(event) == "R"


def test_reasoning_only_message_yields_empty_answer() -> None:
    """
    A real 57-turn session produced exactly this: empty reply text alongside 878
    characters of reasoning. An orchestrator must not read '' as "said nothing".
    """
    event = _message([{"type": "reasoning", "text": "a" * 878}])
    assert dshctl.text_of(event) == ""
    assert len(dshctl.reasoning_of(event)) == 878


def test_text_of_handles_plain_string_content() -> None:
    event = {"type": "user/message", "data": {"message": {"content": "hello"}}}
    assert dshctl.text_of(event) == "hello"


@pytest.mark.parametrize("event", [
    {},
    {"type": "assistant/message"},
    {"type": "assistant/message", "data": {}},
    {"type": "assistant/message", "data": {"message": {}}},
    {"type": "assistant/message", "data": {"message": {"content": None}}},
    {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
])
def test_extractors_tolerate_malformed_events(event: dict) -> None:
    """Never raise on a shape the server might send; return '' instead."""
    assert dshctl.text_of(event) == ""
    assert dshctl.reasoning_of(event) == ""


def test_text_of_skips_non_dict_blocks() -> None:
    event = _message(["not a dict", {"type": "text", "text": "kept"}])  # type: ignore[list-item]
    assert dshctl.text_of(event) == "kept"


# ------------------------------------------------------------ chunk filtering
#
# session.history's maxMessages counts MESSAGES, not events: 3 messages once
# returned 1437 events, 1424 of them assistant/chunk streaming deltas. Leaking
# those into an orchestrator's context is the difference between a readable
# reply and a flooded window.

def test_noise_types_cover_streaming_deltas() -> None:
    assert "assistant/chunk" in dshctl.NOISE_TYPES


def test_reasoning_block_names_are_declared_not_inlined() -> None:
    assert {"reasoning", "thinking"} <= dshctl.REASONING_BLOCKS


# -------------------------------------------------------------- turn-end kind
#
# A turn that died on error, hit max-tokens, or was aborted leaves the session
# `idle` exactly like a clean `completed` one. Reading only `state` cannot tell
# a finished worker from a broken one.

def _patch_events(monkeypatch, events: list[dict]) -> None:
    monkeypatch.setattr(dshctl, "fetch_events",
                        lambda *a, **k: list(events))


def test_last_turn_end_returns_newest_kind(monkeypatch) -> None:
    _patch_events(monkeypatch, [
        {"type": "turn/end", "seq": 10, "data": {"reason": {"kind": "completed"}}},
        {"type": "assistant/message", "seq": 11, "data": {}},
        {"type": "turn/end", "seq": 12, "data": {"reason": {"kind": "max-tokens"}}},
    ])
    assert dshctl.last_turn_end("s", "b") == {"seq": 12, "kind": "max-tokens"}


def test_last_turn_end_preserves_abort_cause(monkeypatch) -> None:
    """`aborted` carries why; dropping the cause loses the whole signal."""
    _patch_events(monkeypatch, [{
        "type": "turn/end", "seq": 7,
        "data": {"reason": {"kind": "aborted", "reason": {"kind": "hook"}}},
    }])
    result = dshctl.last_turn_end("s", "b")
    assert result == {"seq": 7, "kind": "aborted", "reason": {"kind": "hook"}}


def test_last_turn_end_preserves_error_payload(monkeypatch) -> None:
    _patch_events(monkeypatch, [{
        "type": "turn/end", "seq": 3,
        "data": {"reason": {"kind": "error",
                            "error": {"message": "upstream 500", "code": "UNKNOWN"}}},
    }])
    result = dshctl.last_turn_end("s", "b")
    assert result["kind"] == "error"
    assert result["error"]["code"] == "UNKNOWN"


def test_last_turn_end_none_when_absent(monkeypatch) -> None:
    _patch_events(monkeypatch, [{"type": "assistant/message", "seq": 1, "data": {}}])
    assert dshctl.last_turn_end("s", "b") is None


def test_every_documented_turn_end_kind_survives(monkeypatch) -> None:
    """
    The full TurnEndReasonMap from packages/core/session/src/types.ts. If dsh
    adds a variant this stays green -- the point is that none of the known ones
    is silently swallowed.
    """
    for kind in ("completed", "aborted", "blocked", "error",
                 "max-tokens", "interrupted"):
        _patch_events(monkeypatch, [
            {"type": "turn/end", "seq": 1, "data": {"reason": {"kind": kind}}},
        ])
        assert dshctl.last_turn_end("s", "b")["kind"] == kind


# ------------------------------------------------------------- answer batching
#
# One ask() carries many questions and takes ONE batch answer covering all of
# them. Answering them separately is a protocol error, so the builder must
# always produce a list.

class _Args:
    """Stands in for the argparse namespace the cmd_* functions expect."""

    def __init__(self, **kw) -> None:
        # Defaults for the fields build_answers reads whether or not set.
        self.answer = kw.get("answer")
        self.answers = kw.get("answers")
        self.custom = kw.get("custom")
        for key, value in kw.items():
            setattr(self, key, value)


def test_build_answers_from_pairs() -> None:
    args = _Args(answer=["q1=Yes", "q2=No"])
    assert dshctl.build_answers(args) == [
        {"id": "q1", "selected": ["Yes"]},
        {"id": "q2", "selected": ["No"]},
    ]


def test_build_answers_keeps_labels_with_equals_and_spaces() -> None:
    """Option labels are echoed verbatim; only the first '=' separates."""
    args = _Args(answer=["mode=a=b and c"])
    assert dshctl.build_answers(args) == [{"id": "mode", "selected": ["a=b and c"]}]


def test_build_answers_attaches_custom_to_last() -> None:
    args = _Args(answer=["q1=Other"], custom="free text")
    assert dshctl.build_answers(args) == [
        {"id": "q1", "selected": ["Other"], "custom": "free text"},
    ]


def test_build_answers_accepts_full_json_batch() -> None:
    args = _Args(answers='[{"id":"q1","selected":["A"]},{"id":"q2","selected":["B"]}]')
    assert len(dshctl.build_answers(args)) == 2


def test_build_answers_rejects_non_list_json() -> None:
    with pytest.raises(dshctl.RpcError):
        dshctl.build_answers(_Args(answers='{"id":"q1"}'))


def test_build_answers_rejects_pair_without_separator() -> None:
    with pytest.raises(dshctl.RpcError):
        dshctl.build_answers(_Args(answer=["malformed"]))


def test_build_answers_rejects_empty() -> None:
    with pytest.raises(dshctl.RpcError):
        dshctl.build_answers(_Args())


def test_build_answers_rejects_custom_without_question() -> None:
    with pytest.raises(dshctl.RpcError):
        dshctl.build_answers(_Args(custom="orphan"))


# --------------------------------------------------------- WebSocket framing
#
# The running dsh web server serves the event streams as WebSocket upgrades,
# not SSE -- a plain GET answers 426. This reader is hand-rolled to keep the
# runtime stdlib-only, which means its framing is ours to get right.

def _frame(payload: bytes, opcode: int = 0x1, fin: bool = True) -> bytes:
    header = bytes([(0x80 if fin else 0) | opcode])
    length = len(payload)
    if length < 126:
        return header + bytes([length]) + payload
    if length < 1 << 16:
        return header + bytes([126]) + length.to_bytes(2, "big") + payload
    return header + bytes([127]) + length.to_bytes(8, "big") + payload


def _read(raw: bytes) -> list[str]:
    return list(dshctl._ws_frames(io.BytesIO(raw), time.time() + 5))


def test_ws_reads_a_short_text_frame() -> None:
    assert _read(_frame(b'{"a":1}')) == ['{"a":1}']


def test_ws_reads_back_to_back_frames() -> None:
    assert _read(_frame(b"one") + _frame(b"two")) == ["one", "two"]


def test_ws_reads_extended_16bit_length() -> None:
    payload = b"x" * 300  # forces the 126 path
    assert _read(_frame(payload)) == [payload.decode()]


def test_ws_reassembles_continuation_frames() -> None:
    """A frame split across continuations must yield one joined payload."""
    raw = _frame(b"first ", opcode=0x1, fin=False) + _frame(b"second", opcode=0x0)
    assert _read(raw) == ["first second"]


def test_ws_stops_on_close_frame() -> None:
    raw = _frame(b"kept") + _frame(b"", opcode=0x8) + _frame(b"never")
    assert _read(raw) == ["kept"]


def test_ws_skips_ping_and_pong() -> None:
    raw = _frame(b"", opcode=0x9) + _frame(b"real") + _frame(b"", opcode=0xA)
    assert _read(raw) == ["real"]


def test_ws_returns_cleanly_on_truncated_header() -> None:
    assert _read(b"\x81") == []


def test_ws_respects_the_deadline() -> None:
    """An already-expired deadline must not read even one frame."""
    reader = io.BytesIO(_frame(b"late"))
    assert list(dshctl._ws_frames(reader, time.time() - 1)) == []


def test_ws_decodes_utf8_payloads() -> None:
    assert _read(_frame("välj bränsle".encode())) == ["välj bränsle"]


# ------------------------------------------------- answerable frame selection
#
# Only question/requested and approval/requested are answerable; every other
# mux frame is a pure push. Treating a push as answerable would have the
# orchestrator try to respond to, say, a projection update.

def test_answerable_is_exactly_the_two_request_frames() -> None:
    assert dshctl.ANSWERABLE == {"question/requested", "approval/requested"}


@pytest.mark.parametrize("frame_type", [
    "session/event", "session/subscribed", "session/queue", "session/jobs",
    "session/projection", "question/resolved", "approval/resolved",
    "stream/error",
])
def test_pure_pushes_are_not_answerable(frame_type: str) -> None:
    assert frame_type not in dshctl.ANSWERABLE


# ------------------------------------------- unreachable is not the same as empty
#
# Real bug: read_stream swallowed every OSError, so a refused connection
# returned [] and `pending` reported "nothing is blocked" when the truth was
# that nothing had been asked. For an orchestrator deciding whether a worker
# needs help, those two answers are opposites.

def test_unreachable_server_raises_rather_than_reporting_empty() -> None:
    with pytest.raises(dshctl.RpcError, match="cannot reach"):
        dshctl.read_stream("/api/events.mux", "http://127.0.0.1:9", settle=0.4,
                           max_seconds=1.0)


def test_pending_requests_propagates_unreachable() -> None:
    with pytest.raises(dshctl.RpcError, match="cannot reach"):
        dshctl.pending_requests("http://127.0.0.1:9", settle=0.4, max_seconds=1.0)


def test_https_base_is_refused_explicitly() -> None:
    """Silently downgrading to plaintext would be worse than failing."""
    with pytest.raises(dshctl.RpcError, match="wss"):
        dshctl._ws_connect("/api/events.mux", "https://127.0.0.1:3080", 1.0)


def test_pending_requests_keys_on_rpc_id(monkeypatch) -> None:
    """
    The stream replays pending frames on open; a repeated rpcId is the same
    outstanding request, not a second one.
    """
    frame = {
        "type": "server-request", "rpcId": "abc", "method": "question/requested",
        "payload": {"type": "question/requested", "sessionId": "s1",
                    "questions": [{"id": "q1", "question": "which?"}]},
    }
    monkeypatch.setattr(dshctl, "read_stream", lambda *a, **k: [frame, dict(frame)])
    items = dshctl.pending_requests("http://x", 0.1, 0.1)
    assert len(items) == 1
    assert items[0]["rpcId"] == "abc"
    assert items[0]["questions"][0]["id"] == "q1"


def test_pending_requests_keeps_approval_detail(monkeypatch) -> None:
    """An approval stalls a worker until a human acts; the tool name is the point."""
    monkeypatch.setattr(dshctl, "read_stream", lambda *a, **k: [{
        "type": "server-request", "rpcId": "xyz", "method": "approval/requested",
        "payload": {"type": "approval/requested", "sessionId": "s1",
                    "approvalId": "ap1", "toolName": "execute_bash",
                    "reason": "writes outside the workspace"},
    }])
    item = dshctl.pending_requests("http://x", 0.1, 0.1)[0]
    assert item["kind"] == "approval/requested"
    assert item["toolName"] == "execute_bash"


def test_pending_requests_ignores_pure_pushes(monkeypatch) -> None:
    monkeypatch.setattr(dshctl, "read_stream", lambda *a, **k: [{
        "type": "server-request", "rpcId": "p1", "method": "session/projection",
        "payload": {"type": "session/projection", "key": "title", "value": "x"},
    }])
    assert dshctl.pending_requests("http://x", 0.1, 0.1) == []


# ------------------------------------------------------- answer validation
#
# The host validates a batch in `matchesQuestions`
# (packages/host/apiproxy/src/api-proxy.ts) and reports failure as a bare
# `{accepted:false, reason:'bad-response'}` -- no indication of which rule
# broke. These rules are not guessable, and violating one leaves the worker
# still blocked while the call looks like it did something. Each test below is
# one host rule, checked locally so the caller gets a reason.

SINGLE = [{"id": "q1", "question": "pick", "options": [
    {"label": "Yes"}, {"label": "No"}]}]
MULTI = [{"id": "q1", "question": "pick many", "multiSelect": True,
          "options": [{"label": "A"}, {"label": "B"}]}]


def test_valid_single_select_passes() -> None:
    dshctl.validate_answers([{"id": "q1", "selected": ["Yes"]}], SINGLE)


def test_custom_alone_is_valid_for_single_select() -> None:
    """Free text instead of an option is allowed; alongside one is not."""
    dshctl.validate_answers([{"id": "q1", "selected": [], "custom": "other"}], SINGLE)


def test_custom_with_selection_rejected_for_single_select() -> None:
    """The real mistake: both fields set on a single-select question."""
    with pytest.raises(dshctl.RpcError, match="mutually exclusive"):
        dshctl.validate_answers(
            [{"id": "q1", "selected": ["No"], "custom": "and also this"}], SINGLE)


def test_custom_with_selection_allowed_for_multi_select() -> None:
    dshctl.validate_answers(
        [{"id": "q1", "selected": ["A"], "custom": "plus this"}], MULTI)


def test_multiple_labels_rejected_for_single_select() -> None:
    with pytest.raises(dshctl.RpcError, match="at most one"):
        dshctl.validate_answers([{"id": "q1", "selected": ["Yes", "No"]}], SINGLE)


def test_multiple_labels_allowed_for_multi_select() -> None:
    dshctl.validate_answers([{"id": "q1", "selected": ["A", "B"]}], MULTI)


def test_unknown_label_rejected() -> None:
    with pytest.raises(dshctl.RpcError, match="not offered"):
        dshctl.validate_answers([{"id": "q1", "selected": ["Maybe"]}], SINGLE)


def test_label_match_is_exact() -> None:
    """Labels are echoed verbatim — case and whitespace included."""
    with pytest.raises(dshctl.RpcError, match="not offered"):
        dshctl.validate_answers([{"id": "q1", "selected": ["yes"]}], SINGLE)


def test_duplicate_labels_rejected() -> None:
    with pytest.raises(dshctl.RpcError, match="duplicate"):
        dshctl.validate_answers([{"id": "q1", "selected": ["A", "A"]}], MULTI)


def test_blank_custom_rejected() -> None:
    with pytest.raises(dshctl.RpcError, match="must not be blank"):
        dshctl.validate_answers([{"id": "q1", "selected": [], "custom": "   "}], SINGLE)


def test_partial_batch_rejected() -> None:
    """Every question in the ask must be answered, in one batch."""
    two = SINGLE + [{"id": "q2", "question": "second", "options": [{"label": "Ok"}]}]
    with pytest.raises(dshctl.RpcError, match="whole batch"):
        dshctl.validate_answers([{"id": "q1", "selected": ["Yes"]}], two)


def test_answers_are_matched_by_position_not_id() -> None:
    """
    The host zips answers to questions by index and then checks the id, so a
    correct set in the wrong order is rejected. Surprising enough to pin.
    """
    two = SINGLE + [{"id": "q2", "question": "second", "options": [{"label": "Ok"}]}]
    swapped = [{"id": "q2", "selected": ["Ok"]}, {"id": "q1", "selected": ["Yes"]}]
    with pytest.raises(dshctl.RpcError, match="by position"):
        dshctl.validate_answers(swapped, two)


def test_question_without_options_accepts_custom_only() -> None:
    free = [{"id": "q1", "question": "describe"}]
    dshctl.validate_answers([{"id": "q1", "selected": [], "custom": "text"}], free)


# ----------------------------------------------- cancel reports outcome, not receipt
#
# session.cancel returns {accepted:true} unconditionally -- verified identical
# for an idle session and a mid-flight one. An orchestrator trusting the receipt
# can believe it stopped work that had already finished and committed.

def test_cancel_on_an_idle_session_reports_that_nothing_was_stopped(monkeypatch, capsys) -> None:
    monkeypatch.setattr(dshctl, "find_session",
                        lambda *a, **k: {"sessionId": "s1", "running": False})
    monkeypatch.setattr(dshctl, "last_turn_end",
                        lambda *a, **k: {"seq": 10, "kind": "completed"})
    called = []
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: called.append(a) or {})

    dshctl.cmd_cancel(_Args(session="s1", base="http://x", timeout=1.0))
    out = json.loads(capsys.readouterr().out)
    assert out["cancelled"] is False
    assert "already idle" in out["reason"]
    assert called == [], "must not send a cancel for a session that is not running"


def test_cancel_confirms_a_real_abort(monkeypatch, capsys) -> None:
    monkeypatch.setattr(dshctl, "find_session",
                        lambda *a, **k: {"sessionId": "s1", "running": True})
    monkeypatch.setattr(dshctl, "head_seq", lambda *a, **k: 100)
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: {"accepted": True})
    monkeypatch.setattr(dshctl, "wait_for_turn_end", lambda *a, **k: (
        True, {"turnEnd": {"seq": 101, "kind": "aborted",
                           "reason": {"kind": "user"}}}))

    dshctl.cmd_cancel(_Args(session="s1", base="http://x", timeout=1.0))
    out = json.loads(capsys.readouterr().out)
    assert out["cancelled"] is True
    assert out["turnEnd"]["kind"] == "aborted"


def test_cancel_that_loses_the_race_is_not_reported_as_cancelled(monkeypatch, capsys) -> None:
    """The turn finished on its own terms, so its work stands."""
    monkeypatch.setattr(dshctl, "find_session",
                        lambda *a, **k: {"sessionId": "s1", "running": True})
    monkeypatch.setattr(dshctl, "head_seq", lambda *a, **k: 100)
    monkeypatch.setattr(dshctl, "rpc", lambda *a, **k: {"accepted": True})
    monkeypatch.setattr(dshctl, "wait_for_turn_end", lambda *a, **k: (
        True, {"turnEnd": {"seq": 101, "kind": "completed"}}))

    dshctl.cmd_cancel(_Args(session="s1", base="http://x", timeout=1.0))
    out = json.loads(capsys.readouterr().out)
    assert out["cancelled"] is False
    assert "finished before" in out["note"]
    assert out["receipt"] == {"accepted": True}, "receipt kept, just not trusted"


# ---------------------------------------------------------------- session naming

def test_name_surfaces_normalisation(monkeypatch, capsys) -> None:
    """A silently truncated or stripped title would misidentify a worker."""
    monkeypatch.setattr(dshctl, "find_session", lambda *a, **k: {"sessionId": "s1"})
    monkeypatch.setattr(dshctl, "rpc",
                        lambda *a, **k: {"title": "implementer", "seq": 7})
    dshctl.cmd_name(_Args(session="s1", base="http://x",
                          title="implementer\n\nand more text"))
    out = json.loads(capsys.readouterr().out)
    assert out["title"] == "implementer"
    assert "note" in out


def test_name_is_quiet_when_accepted_verbatim(monkeypatch, capsys) -> None:
    monkeypatch.setattr(dshctl, "find_session", lambda *a, **k: {"sessionId": "s1"})
    monkeypatch.setattr(dshctl, "rpc",
                        lambda *a, **k: {"title": "implementer", "seq": 7})
    dshctl.cmd_name(_Args(session="s1", base="http://x", title="implementer"))
    out = json.loads(capsys.readouterr().out)
    assert "note" not in out
    assert out["seq"] == 7
