"""dsh-wake: the decision rules, the loop with injected time and runner, and quoting."""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from dsh_workers_mcp import wake


def args(tmp_path: Path, **over) -> argparse.Namespace:
    base = dict(cmd="client --resume {prompt}", prompt="go on", state_dir=str(tmp_path),
                milestone=None, interval=0, budget_hours=8.0, max_fires=50, dry_run=False)
    return argparse.Namespace(**{**base, **over})


def entries(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "wake.jsonl").read_text().splitlines()]


def decision(needs, **over):
    base = dict(last_needs=None, fires=0, elapsed_hours=0.0, budget_hours=8.0, max_fires=50,
                milestone=None)
    return wake.decide(needs, **{**base, **over})


def test_wake_only_when_something_needs_the_lead() -> None:
    assert decision([])[0] == "skip" and "nothing needs" in decision([])[1]
    assert decision(None) == ("skip", "dsh_brief failed")
    action, reason = decision(["verdict on T-001: returned 2h ago", "answer for T-002: blocked 1h"])
    assert action == "fire" and "T-001" in reason and "T-002" in reason


def test_the_same_items_as_at_the_last_wake_do_not_wake_again() -> None:
    needs = ["verdict on T-001: returned 2h ago"]
    assert decision(needs, last_needs=list(needs))[0] == "skip"
    assert decision(needs + ["x"], last_needs=needs)[0] == "fire"          # a new item
    assert decision(["y"], last_needs=needs)[0] == "fire"                  # a different item


def test_the_budget_the_wake_count_and_the_milestone_stop_the_driver() -> None:
    assert decision(["x"], elapsed_hours=8.0)[0] == "stop"
    assert decision(["x"], fires=50)[0] == "stop"
    assert decision(["x"], milestone="DONE") == ("stop", "the milestone file says DONE")
    assert decision([], milestone="BLOCKED")[0] == "stop"                  # stop beats skip


@pytest.mark.parametrize("text, expected", [
    ("DONE\nrest", "DONE"), ("\n\n# Blocked on the user\n", "BLOCKED"), ("- done: all", "DONE"),
    ("In progress\nDONE later", None), ("", None)])
def test_milestone_file_first_line(tmp_path: Path, text: str, expected) -> None:
    path = tmp_path / "MILESTONE.md"
    path.write_text(text)
    assert wake.milestone_stop(path) == expected
    assert wake.milestone_stop(tmp_path / "missing.md") is None and wake.milestone_stop(None) is None


def test_the_loop_fires_on_new_items_skips_repeats_and_stops_at_the_wake_limit(tmp_path: Path) -> None:
    polls = iter([["a"], ["a"], [], ["a", "b"], ["a", "b"]])
    ran: list[str] = []
    code = wake.run(args(tmp_path, max_fires=2), get_needs=lambda: next(polls),
                    runner=lambda cmd: ran.append(cmd) or 0, sleep=lambda _s: None)
    assert code == 0
    assert ran == ["client --resume 'go on'", "client --resume 'go on'"]
    assert [(e["action"], e["reason"]) for e in entries(tmp_path)] == [
        ("fire", "a"), ("ended", "the command exited with 0"),
        ("skip", "the same items as at the last wake"),
        ("skip", "nothing needs the lead"),
        ("fire", "a; b"), ("ended", "the command exited with 0"),
        ("stop", "budget: 2 wakes done")]


def test_the_loop_stops_when_the_time_budget_is_spent(tmp_path: Path) -> None:
    ticks = iter([0.0, 1000.0, 3600.0 * 3])               # start, first check, second check
    wake.run(args(tmp_path, budget_hours=2.0), get_needs=lambda: [],
             runner=lambda cmd: 0, sleep=lambda _s: None, clock=lambda: next(ticks))
    assert [e["action"] for e in entries(tmp_path)] == ["skip", "stop"]


def test_the_loop_stops_when_the_milestone_file_says_done(tmp_path: Path) -> None:
    milestone = tmp_path / "MILESTONE.md"
    milestone.write_text("In progress\n")
    ran: list[str] = []

    def run_client(cmd: str) -> int:
        ran.append(cmd)
        milestone.write_text("DONE\n")                    # the woken lead finishes the milestone
        return 0

    wake.run(args(tmp_path, milestone=str(milestone)), get_needs=lambda: ["a"],
             runner=run_client, sleep=lambda _s: None)
    assert len(ran) == 1 and entries(tmp_path)[-1]["action"] == "stop"


def test_a_failed_read_is_a_skip_and_the_loop_goes_on(tmp_path: Path) -> None:
    polls = iter([None, ["a"], ["a"]])
    ran: list[str] = []
    wake.run(args(tmp_path, max_fires=1), get_needs=lambda: next(polls),
             runner=lambda cmd: ran.append(cmd) or 0, sleep=lambda _s: None)
    assert [e["action"] for e in entries(tmp_path)][:2] == ["skip", "fire"] and len(ran) == 1


def test_dry_run_makes_one_pass_and_runs_nothing(tmp_path: Path, capsys) -> None:
    ran: list[str] = []
    code = wake.run(args(tmp_path, dry_run=True), get_needs=lambda: ["a"],
                    runner=lambda cmd: ran.append(cmd) or 0, sleep=lambda _s: pytest.fail("slept"))
    assert code == 0 and ran == []
    assert "would run: client --resume 'go on'" in capsys.readouterr().out
    assert entries(tmp_path) == [{**entries(tmp_path)[0], "action": "fire", "reason": "a", "dryRun": True}]


def test_a_failing_command_is_logged_and_does_not_stop_the_driver(tmp_path: Path) -> None:
    polls = iter([["a"], ["b"], ["c"]])
    wake.run(args(tmp_path, max_fires=2), get_needs=lambda: next(polls),
             runner=lambda cmd: 3, sleep=lambda _s: None)
    assert [e["exit"] for e in entries(tmp_path) if e["action"] == "ended"] == [3, 3]


def test_the_prompt_is_quoted_for_the_shell(tmp_path: Path) -> None:
    prompt = "it's \"done\"; $(touch pwned)"
    command = wake.render("printf %s {prompt}", prompt, tmp_path)
    out = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=tmp_path)
    assert out.stdout == prompt and not (tmp_path / "pwned").exists()
    assert str(tmp_path) in wake.render("cd {state_dir}", "x", tmp_path)
