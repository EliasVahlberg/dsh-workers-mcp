"""
docs/QUICKSTART.md as a test: the same steps, against the real server process, a real git
repository and worktree, and a fake dsh behind a real HTTP server. A scripted "worker"
commits a file when it gets a prompt. No model and no real dsh is involved.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from fake_http import FakeHttpDsh
from fakes import FakeDsh


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@example.test",
                           *args], capture_output=True, text=True, check=True).stdout.strip()


class ScriptedWorker(FakeDsh):
    """A dsh whose worker does its task when the test calls `finish`: it commits a file and ends the turn."""

    def rpc(self, method: str, payload: dict | None = None, timeout: float = 120.0) -> dict:
        value = super().rpc(method, payload, timeout)
        if method == "session.create":
            self.sessions[value["sessionId"]]["cwd"] = (payload or {})["cwd"]
        return value

    def finish(self, sid: str) -> None:
        cwd = Path(self.sessions[sid]["cwd"])
        (cwd / "hello.txt").write_text("hello\n")
        git(cwd, "add", "hello.txt")
        git(cwd, "commit", "-m", "add hello.txt")
        self.emit(sid, "assistant/message", {"turn": 1, "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Added hello.txt and committed it."}]}})
        self.emit(sid, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        self.sessions[sid]["running"] = False


class Server:
    """The MCP server as a client starts it: a child process that speaks JSON-RPC lines on stdio."""

    def __init__(self, base: str, state_dir: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "dsh_workers_mcp.server", "--base", base, "--state-dir", str(state_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method,
                                          "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def tool(self, name: str, **arguments) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments})["result"]
        text = result["content"][0]["text"]
        assert result["isError"] is False, f"{name}: {text}"
        return text

    def close(self) -> None:
        assert self.proc.stdin
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def project(tmp_path):
    """Step 1 of the guide: a small repository with a worktree for one worker."""
    repo = tmp_path / "demo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "README.md").write_text("demo\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "start")
    worktree = tmp_path / "demo-w1"
    git(repo, "worktree", "add", "-b", "w1", str(worktree))
    return repo, worktree


def test_the_quick_start_reaches_a_merged_task(project, tmp_path) -> None:
    repo, worktree = project
    state_dir = tmp_path / ".orchestrator"
    worker = ScriptedWorker()
    with FakeHttpDsh(worker) as dsh:
        server = Server(dsh.base, state_dir)
        try:
            # Step 2: the client lists the tools.
            names = {t["name"] for t in server.request("tools/list")["result"]["tools"]}
            assert {"dsh_create", "dsh_task", "dsh_wait", "dsh_tasks", "dsh_digest", "dsh_brief"} <= names

            # Step 3: create a worker in the worktree, then name its role.
            created = json.loads(server.tool("dsh_create", cwd=str(worktree)))
            session = created["sessionId"]
            server.tool("dsh_roster", action="set", session=session, role="implementer: hello file")

            # Step 4: record the task, then dispatch it to the worker.
            task = json.loads(server.tool(
                "dsh_tasks", action="add", title="Add hello.txt", worktree=str(worktree),
                branch="w1", acceptance="hello.txt exists and is committed on w1"))
            task_id = task["id"]
            server.tool("dsh_task", session=session, task_id=task_id,
                        prompt="Create hello.txt with the text hello and commit it.")

            # Step 5: the worker works. Wait for the turn to end, then read the digest.
            worker.finish(session)
            waited = json.loads(server.tool("dsh_wait", session=session, timeout=30))
            assert waited.get("completed") is True or waited.get("alreadyIdle") or "turnEnd" in waited, waited
            digest = json.loads(server.tool("dsh_digest", session=session))
            assert digest["git"]["commits"] == 1 and digest["git"]["files"] == ["hello.txt"], digest

            # Step 6: review the work, merge it, and close the task. A close needs a note.
            assert (worktree / "hello.txt").read_text() == "hello\n"
            git(repo, "merge", "--no-edit", "w1")
            commit = git(repo, "rev-parse", "--short", "HEAD")
            refused = server.request("tools/call", {"name": "dsh_tasks", "arguments": {
                "action": "update", "id": task_id, "status": "merged"}})["result"]
            assert refused["isError"] is True and "needs a note" in refused["content"][0]["text"]
            closed = json.loads(server.tool("dsh_tasks", action="update", id=task_id, status="merged",
                                            note=commit))
            assert closed["status"] == "merged"

            # Step 7: no task is open. The brief offers the idle worker new work.
            brief = json.loads(server.tool("dsh_brief"))
            assert brief["openTasks"] == []
            assert len(brief["needsAction"]) == 1 and "idle, no task" in brief["needsAction"][0], brief
        finally:
            server.close()
    assert (repo / "hello.txt").exists()
