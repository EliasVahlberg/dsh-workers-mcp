"""
The files of a state directory and how to read and write them.

`Store` is the base of `State`. It knows where each file is, how to lock and
replace it atomically, and how to read the three documents (roster, tasks and a
session's state). It makes no decision about their content. The rules live in
`roster.py`, `history.py` and `tasks.py`.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import os
import tempfile
from pathlib import Path

try:  # POSIX advisory locks; two orchestrator sessions may share a state dir
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX: best effort, no locking
    fcntl = None  # type: ignore[assignment]

from dsh_workers_mcp.transport import RpcError
from dsh_workers_mcp.transport import DshClient, HttpClient

DEFAULT_DIRNAME = ".orchestrator"
LOG_RESULT_CHARS = 20_000   # per logged call; the full text is in dsh anyway


# ------------------------------------------------------------------ helpers

def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ms_to_iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    return _dt.datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def resolve_state_dir(explicit: str | None, cwd: str | None = None) -> Path:
    """`--state-dir` when given, else <cwd>/.orchestrator (cwd = where the server runs)."""
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path(cwd or os.getcwd()).resolve() / DEFAULT_DIRNAME


def _write_json(path: Path, data) -> None:
    """Atomic replace: a crash mid-write leaves the old file, never half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


@contextlib.contextmanager
def _locked(path: Path):
    """Exclusive advisory lock on `path` for a read-modify-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        if fcntl is not None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_UN)


def short(session_id: str) -> str:
    return session_id.removeprefix("session-")[:8]



class Store:
    """One orchestrator state directory. Created lazily on first write."""

    def __init__(self, root: Path | str, client: DshClient | None = None):
        self.root = Path(root)
        self.client: DshClient = client or HttpClient()

    # paths
    @property
    def roster_path(self) -> Path:
        return self.root / "roster.json"

    @property
    def tasks_path(self) -> Path:
        return self.root / "tasks.json"

    def session_dir(self, session_id: str) -> Path:
        return self.root / "history" / session_id

    def _lock(self, name: str):
        return _locked(self.root / ".locks" / f"{name}.lock")

    # ------------------------------------------------------------ call log

    def log_call(self, tool: str, arguments: dict, text: str, ok: bool,
                 duration_ms: int, state_error: str | None = None) -> None:
        """Append one tools/call record. Never raises into the caller's path."""
        record = {
            "at": now_iso(), "tool": tool, "arguments": arguments, "ok": ok,
            "durationMs": duration_ms, "resultChars": len(text),
            "result": text[:LOG_RESULT_CHARS],
            **({"resultTruncated": True} if len(text) > LOG_RESULT_CHARS else {}),
            **({"stateError": state_error} if state_error else {}),
        }
        path = self.root / "logs" / f"{_dt.date.today().isoformat()}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # One write() on an O_APPEND file: lines from concurrent servers do not interleave
        # for records this size on Linux.
        with open(path, "a") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    def roster(self) -> dict:
        return _read_json(self.roster_path, {"version": 1, "workers": {}})

    def is_managed(self, session_id: str) -> bool:
        return session_id in self.roster()["workers"]

    def _update_worker(self, session_id: str, auto: dict | None = None,
                       notes: dict | None = None, create: bool = False) -> dict:
        with self._lock("roster"):
            roster = self.roster()
            workers = roster["workers"]
            if session_id not in workers:
                if not create:
                    raise RpcError(
                        f"{session_id} is not on the roster — adopt it first "
                        f"(dsh_roster action=adopt)")
                workers[session_id] = {
                    "auto": {"sessionId": session_id, "short": short(session_id),
                             "addedAt": now_iso()},
                    "notes": {"role": None, "retired": False, "log": []},
                }
            worker = workers[session_id]
            if auto:
                worker["auto"].update(auto)
                worker["auto"]["observedAt"] = now_iso()
            if notes:
                worker["notes"].update(notes)
            _write_json(self.roster_path, roster)
            return worker

    def session_state(self, session_id: str) -> dict:
        return _read_json(self.session_dir(session_id) / "state.json", {})


    def tasks(self) -> dict:
        return _read_json(self.tasks_path, {"version": 1, "nextId": 1, "tasks": {}})

    def _save_tasks(self, data: dict) -> None:
        _write_json(self.tasks_path, data)

