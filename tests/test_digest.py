"""The mechanical digest: shell parsing, check detection, the digest over events, git."""

import json
import subprocess

import pytest

from dsh_workers_mcp import digest


# ------------------------------------------------------------- shell parsing

def _flags(command: str) -> list[tuple[str, bool, bool]]:
    return [(s["verb"], s["writes"], s["opaque"]) for s in digest.analyze(command)]


@pytest.mark.parametrize("command, expected", [
    ("pwd && ls", [("pwd", False, False), ("ls", False, False)]),
    ("cd /w/x && sed -i 's/a/b/' f.txt", [("cd", False, False), ("sed", True, False)]),
    ("sed -n '1,5p' f.txt", [("sed", False, False)]),
    ('grep ">" file', [("grep", False, False)]),            # a quoted > is not a redirect
    ("echo 'a > b'", [("echo", False, False)]),
    ("echo hi > out.txt", [("echo", True, False)]),
    ("echo hi >> out.txt", [("echo", True, False)]),
    ("echo hi > /dev/null 2>&1", [("echo", False, False)]),
    ("cmd 2>err.log", [("cmd", True, False)]),
    ("make test 2>&1 | tail -30", [("make", False, False), ("tail", False, False)]),
    ("cat <<'EOF' > f.py\nprint(1)\nEOF", [("cat", True, False)]),
    ("python3 - <<'PY'\nopen('x','w').write('a')\nPY", [("python3", False, True)]),
    ('python3 -c "print(1)"', [("python3", False, True)]),
    ("python3 tests/run.py", [("python3", False, False)]),   # a script file: not guessed
    ("bash -c 'rm -rf build && make'", [("bash", True, False)]),
    ("FOO=1 timeout 60 rm -f x", [("rm", True, False)]),
    ("timeout -s KILL 10 touch x", [("touch", True, False)]),
    ("git status && git diff --stat main...", [("git", False, False), ("git", False, False)]),
    ("git -C /w checkout -- f", [("git", True, False)]),
    ("git stash list", [("git", False, False)]),
    ("git stash", [("git", True, False)]),
    ("git commit -m x", [("git", False, False)]),
    ("find . -name '*.o' -delete", [("find", True, False)]),
    ("find . -name '*.o'", [("find", False, False)]),
    ("ls | xargs rm", [("ls", False, False), ("xargs", True, False)]),
    ("perl -pi -e 's/a/b/' f", [("perl", True, False)]),
    ("perl -Mstrict -e 'print 1'", [("perl", False, True)]),
    ("curl -sS -o /tmp/x.jar https://h/x", [("curl", True, False)]),
    ("curl -sS https://h/x", [("curl", False, False)]),
    ("tee out.log", [("tee", True, False)]),
    ("echo $((1<<3))", [("echo", False, False), ("1", False, False)]),  # not a heredoc
    ("# just a comment", []),
    ("echo 'unbalanced", [("echo", False, False)]),          # falls back, does not raise
])
def test_analyze_classifies_the_usual_commands(command, expected) -> None:
    assert _flags(command) == expected


@pytest.mark.parametrize("command, via", [
    ("pytest -q tests/", "pattern"),
    ("python -m pytest", "pattern"),
    ("python3.11 -m unittest discover", "pattern"),
    ("make test", "pattern"),
    ("make -j4 check", "pattern"),
    ("./gradlew test --tests Foo", "pattern"),
    ("npm run test", "pattern"),
    ("cargo test -p x", "pattern"),
    ("go vet ./...", "pattern"),
    ("ruff check .", "pattern"),
    ("make build", None),
    ("pip install pytest", None),            # mentions a check, is not one
    ("cat tests/test_x.py", None),
    ("grep -rn 'make test' docs", None),
    ("git log --oneline", None),
])
def test_default_checks(command, via) -> None:
    results = [digest.check_via(s, [], []) for s in digest.analyze(command)]
    assert (next((r for r in results if r), None)) == via


def test_acceptance_spans_and_project_patterns_define_checks() -> None:
    spans = digest.check_spans("`make qa-world` builds it and `src/Foo.java` is touched; "
                               "`README.md updated` and `pytest -k belt` pass")
    assert spans == ["make qa-world", "pytest -k belt"]       # a file name is not a command
    seg = digest.analyze("cd w && make qa-world 2>&1 | tail")[1]
    assert digest.check_via(seg, [], []) is None
    assert digest.check_via(seg, [], spans) == "acceptance"
    assert digest.check_via(seg, [r"^make qa-"], []) == "pattern"
    read_only = digest.analyze("grep 'make qa-world' notes.txt")[0]
    assert digest.check_via(read_only, [], spans) is None     # reading it is not running it


# ------------------------------------------------------------------ the digest

def ev(seq: int, kind: str, turn: int, **data) -> dict:
    return {"seq": seq, "type": kind, "time": 0, "data": {"turn": turn, **data}}


class Log:
    """Builds a mirrored event log the way dsh writes one."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.n = 0

    def call(self, turn: int, name: str, args: dict, result: str, error: bool = False) -> None:
        self.n += 1
        cid = f"call_{self.n}"
        self.events.append(ev(self.n * 10, "tool/call", turn, step=1, callId=cid, name=name,
                              arguments=json.dumps(args)))
        self.events.append(ev(self.n * 10 + 1, "tool/result", turn, step=1, message={
            "source": {"kind": "tool", "callId": cid},
            "content": [{"type": "tool-result", "toolCallId": cid, "isError": error,
                         "content": [{"type": "text", "text": result}]}]}))

    def turn(self, turn: int, ended: str = "completed") -> None:
        self.n += 1
        self.events.append(ev(self.n * 10, "step/start", turn, step=1))
        self.events.append(ev(self.n * 10 + 1, "turn/end", turn, reason={"kind": ended}))


def test_digest_lists_edits_commands_and_checks() -> None:
    log = Log()
    log.call(1, "edit", {"file_path": "/w/src/A.java"}, "updated")
    log.call(1, "edit", {"file_path": "/w/src/A.java"}, "updated")
    log.call(1, "write", {"file_path": "/w/tests/t.py"}, "ok")
    log.call(1, "edit", {"file_path": "/w/src/B.java"}, "Error: read it first", error=True)
    log.call(1, "bash", {"command": "cd /w && make test 2>&1 | tail -3"},
             "12 passed in 1.2s\n")
    log.turn(1)
    d = digest.build(log.events, worktree="/w")
    assert d["edited"] == ["src/A.java x2", "tests/t.py"]       # the failed edit is not counted
    assert d["tools"] == {"edit": 3, "bash": 1, "write": 1}
    assert d["checks"] == [{"cmd": "make test 2>&1 | tail -3", "ok": True, "seq": 50,
                            "via": "pattern", "tail": "12 passed in 1.2s"}]
    assert d["verification"] == {"checkedAfterLastEdit": True, "lastCheckOk": True}
    assert d["errors"]["count"] == 1 and d["ended"] == "completed" and d["steps"] == 1


def test_no_check_after_the_last_edit_is_visible() -> None:
    log = Log()
    log.call(1, "bash", {"command": "pytest -q"}, "3 passed in 0.1s")
    log.call(1, "edit", {"file_path": "/w/a.py"}, "ok")
    d = digest.build(log.events)
    assert d["verification"]["checkedAfterLastEdit"] is False
    log2 = Log()
    log2.call(1, "edit", {"file_path": "/w/a.py"}, "ok")
    assert digest.build(log2.events)["verification"]["note"] == "no command matched as a check"
    assert digest.build(log2.events)["verification"]["checkedAfterLastEdit"] is False


def test_a_shell_write_counts_as_an_edit_for_that_comparison() -> None:
    log = Log()
    log.call(1, "bash", {"command": "pytest -q"}, "3 passed in 0.1s")
    log.call(1, "bash", {"command": "sed -i 's/a/b/' src/x.py"}, "")
    d = digest.build(log.events)
    assert d["shell"]["writes"] == 1 and d["verification"]["checkedAfterLastEdit"] is False


def test_exit_lines_pipelines_and_background_jobs() -> None:
    log = Log()
    log.call(1, "bash", {"command": "pytest -x"}, "boom\n[exit code: 1]")
    log.call(1, "bash", {"command": "make test 2>&1 | tail"},
             "FAILED t.py::t\n1 failed, 9 passed in 2s\nmake: *** [Makefile:3: test] Error 1\n")
    log.call(1, "bash", {"command": "make pytest", "run_in_background": True},
             "started background job bash-7")
    log.call(1, "job_output", {"job_id": "bash-7"},
             "...\n141 passed in 54s\n[status: completed, exit code: 0]")
    log.call(1, "bash", {"command": "curl -sS http://nope"}, "(no output)\n[exit code: 7]")
    d = digest.build(log.events)
    first, piped, job = d["checks"]
    assert first["ok"] is False and first["exit"] == 1
    assert piped["ok"] is False and piped["outputFailed"] is True and "exit" not in piped
    assert job["ok"] is True and job["tail"] == "141 passed in 54s"
    assert d["failed"] == [{"cmd": "curl -sS http://nope", "ok": False, "exit": 7, "seq": 50,
                            "tail": "(no output)"}]


def test_a_running_job_is_neither_ok_nor_failed() -> None:
    log = Log()
    log.call(1, "bash", {"command": "make test", "run_in_background": True},
             "started background job bash-1")
    log.call(1, "job_output", {"job_id": "bash-1"}, "so far\n[status: running]")
    assert digest.build(log.events)["checks"][0]["ok"] is None


def test_refusals_and_errors_are_counted_apart() -> None:
    log = Log()
    log.call(1, "bash", {"command": "cat <<EOF > f\nx\nEOF"},
             "Error: write files with the write or edit tool [tool-policy: preset supervised]",
             error=True)
    log.call(1, "edit", {"file_path": "/w/a"}, "Error: read first", error=True)
    errors = digest.build(log.events)["errors"]
    assert errors["count"] == 2 and errors["refused"] == 1


def test_writes_outside_the_worktree_are_reported() -> None:
    log = Log()
    log.call(1, "edit", {"file_path": "/w/in.py"}, "ok")
    log.call(1, "edit", {"file_path": "/main/out.py"}, "ok")
    d = digest.build(log.events, worktree="/w")
    assert d["edited"] == ["in.py", "/main/out.py"]
    assert d["editedOutsideWorktree"] == ["/main/out.py"]


def test_paths_share_one_root_when_there_is_no_worktree() -> None:
    log = Log()
    log.call(1, "edit", {"file_path": "/home/u/proj/wt/src/a.py"}, "ok")
    log.call(1, "edit", {"file_path": "/home/u/proj/wt/tests/b.py"}, "ok")
    d = digest.build(log.events)
    assert d["root"] == "/home/u/proj/wt" and d["edited"] == ["src/a.py", "tests/b.py"]


def test_the_window_is_the_last_n_turns() -> None:
    log = Log()
    log.call(1, "edit", {"file_path": "/w/old.py"}, "ok")
    log.turn(1)
    log.call(2, "edit", {"file_path": "/w/mid.py"}, "ok")
    log.turn(2, "error")
    log.call(3, "edit", {"file_path": "/w/new.py"}, "ok")
    log.turn(3)
    assert digest.build(log.events, worktree="/w")["edited"] == ["new.py"]
    two = digest.build(log.events, turns=2, worktree="/w")
    assert two["edited"] == ["mid.py", "new.py"] and two["turns"] == [2, 3]
    assert digest.build(log.events, turns=9, worktree="/w")["turns"] == [1, 3]
    assert "no turns" in digest.build([])["note"]


def test_the_digest_is_deterministic_and_small() -> None:
    log = Log()
    for i in range(40):
        log.call(1, "edit", {"file_path": f"/w/src/pkg/F{i}.java"}, "ok")
        log.call(1, "bash", {"command": f"cd /w && grep -n x src/F{i}.java | head"}, "x")
    log.call(1, "bash", {"command": "cd /w && make test 2>&1 | tail -5"}, "40 passed in 3s")
    log.turn(1)
    first = digest.build(log.events, worktree="/w")
    assert first == digest.build(log.events, worktree="/w")
    assert len(json.dumps(first)) < 1500
    assert first["edited"][-1] == "+30 more"


# ------------------------------------------------------------------------- git

def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "a.txt").write_text("one\n")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def test_git_changes_sees_edits_made_by_any_means(repo) -> None:
    before = digest.git_snapshot(str(repo))
    assert before["dirty"] == [] and before["untracked"] == []
    (repo / "a.txt").write_text("one\ntwo\n")                  # an edit
    (repo / "new.py").write_text("print(1)\n")                 # a new, untracked file
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "second")
    (repo / "b.txt").write_text("b\n")
    after = digest.git_changes(str(repo), before)
    assert after["commits"] == 1 and after["filesChanged"] == 3
    assert after["files"] == ["a.txt", "b.txt", "new.py"] and after["insertions"] == 1
    assert after["uncommitted"] == 2                            # the two untracked files


def test_git_without_a_repo_or_a_snapshot_returns_none(tmp_path) -> None:
    assert digest.git_snapshot(str(tmp_path)) is None
    assert digest.git_snapshot(None) is None
    assert digest.git_changes(str(tmp_path), None) is None
    assert digest.git_changes(None, {"head": "x"}) is None


def test_untracked_files_present_at_dispatch_are_not_attributed(repo) -> None:
    (repo / "old_note.txt").write_text("x")
    before = digest.git_snapshot(str(repo))
    (repo / "mine.txt").write_text("y")
    assert digest.git_changes(str(repo), before)["files"] == ["mine.txt"]
