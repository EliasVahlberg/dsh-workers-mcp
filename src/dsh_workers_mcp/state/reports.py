"""The digest of a managed worker (`dsh_digest`): its mirrored events, its task and its worktree."""

from __future__ import annotations

import contextlib
import json
import re

from dsh_workers_mcp import digest as digest_mod
from dsh_workers_mcp import sessions
from dsh_workers_mcp.transport import RpcError
from dsh_workers_mcp.state.core import State


def _mirrored_events(state: State, session_id: str) -> list[dict]:
    path = state.session_dir(session_id) / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _check_patterns(state: State) -> tuple[list[str], list[str]]:
    """The project's extra check regexes from `checks.txt`, as (valid, invalid)."""
    patterns: list[str] = []
    bad: list[str] = []
    checks_file = state.root / "checks.txt"
    lines = checks_file.read_text().splitlines() if checks_file.exists() else []
    for line in (raw.strip() for raw in lines):
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
            patterns.append(line)
        except re.error:
            bad.append(line)
    return patterns, bad


def _digest_task(state: State, session_id: str, task_id: str | None) -> dict | None:
    """The task that a digest is about: the one named, else the worker's current or last."""
    auto = (state.roster()["workers"].get(session_id) or {}).get("auto", {})
    task_id = task_id or auto.get("currentTask") or auto.get("lastTask")
    if not task_id:
        return None
    task = state.tasks()["tasks"].get(task_id)
    if task is None:
        raise RpcError(f"no task {task_id!r}")
    return task


def digest_for(state: State, session_id: str, turns: int = 1,
               task_id: str | None = None, observe: bool = True) -> dict:
    """The digest of a managed worker's last `turns` turns (see digest.py)."""
    if not state.is_managed(session_id):
        raise RpcError(f"{session_id} is not on the roster; adopt it first "
                              f"(dsh_roster action=adopt)")
    if observe:  # bring the mirror up to date; if dsh is down, use what is mirrored
        with contextlib.suppress(RpcError):
            state.observe(session_id)
    task = _digest_task(state, session_id, task_id)
    worktree = (task or {}).get("worktree")
    patterns, bad = _check_patterns(state)
    result = digest_mod.build(_mirrored_events(state, session_id), turns=max(int(turns), 1),
                              worktree=worktree, acceptance=(task or {}).get("acceptance"),
                              patterns=patterns)
    if task:
        result = {"task": task["id"], **result}
        dispatches = [d for d in task.get("dispatches", []) if d.get("gitBefore")]
        git = digest_mod.git_changes(worktree, dispatches[-1]["gitBefore"]) if dispatches else None
        if git:
            result["git"] = git
    if bad:
        result["badPatterns"] = bad
    return result


def cmd_digest(state: State, arguments: dict) -> dict:
    if not arguments.get("session"):
        raise RpcError("dsh_digest needs a session")
    session_id = sessions.find_session(arguments["session"], state.client)["sessionId"]
    return digest_for(state, session_id, arguments.get("turns", 1), arguments.get("task_id"))

