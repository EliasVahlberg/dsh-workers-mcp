"""scripts/size-gate.sh: the optional pre-commit size limit, tested in throwaway repos."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "size-gate.sh"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", str(cwd), "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, env=env, check=check)


def gate(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, capture_output=True, text=True)


def write_lines(path: Path, n: int, prefix: str = "line") -> None:
    path.write_text("".join(f"{prefix} {i}\n" for i in range(n)))


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "main"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "base.txt").write_text("base\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    assert gate(root, "install").returncode == 0
    return root


def worktree(repo: Path, name: str) -> Path:
    path = repo.parent / name
    git(repo, "worktree", "add", "-q", "-b", name, str(path))
    return path


def test_a_commit_under_the_limit_passes_and_one_over_it_is_refused(repo: Path) -> None:
    wt = worktree(repo, "t1")
    assert gate(wt, "set", "--lines", "10").returncode == 0
    write_lines(wt / "a.txt", 6)
    git(wt, "add", "-A")
    assert git(wt, "commit", "-q", "-m", "small", check=False).returncode == 0
    write_lines(wt / "b.txt", 8)                       # 6 + 8 = 14 changed lines in total
    git(wt, "add", "-A")
    refused = git(wt, "commit", "-q", "-m", "too big", check=False)
    assert refused.returncode != 0
    assert "14 changed lines" in refused.stderr and "limit for this task is 10" in refused.stderr
    assert "Report the total" in refused.stderr


def test_the_lead_raising_the_limit_lets_the_commit_through(repo: Path) -> None:
    wt = worktree(repo, "t1")
    gate(wt, "set", "--lines", "5")
    write_lines(wt / "a.txt", 9)
    git(wt, "add", "-A")
    assert git(wt, "commit", "-q", "-m", "x", check=False).returncode != 0
    gate(wt, "set", "--lines", "20")
    assert git(wt, "commit", "-q", "-m", "x", check=False).returncode == 0


def test_the_file_limit_is_checked_too(repo: Path) -> None:
    wt = worktree(repo, "t1")
    gate(wt, "set", "--lines", "1000", "--files", "2")
    for name in ("a", "b", "c"):
        write_lines(wt / f"{name}.txt", 1)
    git(wt, "add", "-A")
    refused = git(wt, "commit", "-q", "-m", "x", check=False)
    assert refused.returncode != 0 and "3 files" in refused.stderr


def test_a_worktree_with_no_limit_is_not_checked_and_limits_do_not_leak(repo: Path) -> None:
    free, boxed = worktree(repo, "free"), worktree(repo, "boxed")
    gate(boxed, "set", "--lines", "2")
    write_lines(free / "a.txt", 50)
    git(free, "add", "-A")
    assert git(free, "commit", "-q", "-m", "x", check=False).returncode == 0
    write_lines(boxed / "a.txt", 50)
    git(boxed, "add", "-A")
    assert git(boxed, "commit", "-q", "-m", "x", check=False).returncode != 0


def test_deleted_lines_count_and_clear_removes_the_limit(repo: Path) -> None:
    wt = worktree(repo, "t1")
    gate(wt, "set", "--lines", "3")
    (wt / "base.txt").write_text("a\nb\nc\nd\n")        # 1 deleted + 4 added = 5
    git(wt, "add", "-A")
    assert git(wt, "commit", "-q", "-m", "x", check=False).returncode != 0
    assert gate(wt, "clear").returncode == 0
    assert git(wt, "commit", "-q", "-m", "x", check=False).returncode == 0


def test_a_broken_limit_file_or_base_fails_closed_with_a_message(repo: Path) -> None:
    wt = worktree(repo, "t1")
    gate(wt, "set", "--lines", "10", "--base", "no-such-branch")
    write_lines(wt / "a.txt", 1)
    git(wt, "add", "-A")
    refused = git(wt, "commit", "-q", "-m", "x", check=False)
    assert refused.returncode != 0 and "merge-base" in refused.stderr
    assert gate(wt, "set").returncode != 0              # --lines is required


def test_install_does_not_overwrite_a_hook_that_is_not_ours(tmp_path: Path) -> None:
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 0\n")
    refused = gate(root, "install")
    assert refused.returncode != 0 and "already exists" in refused.stderr
    assert hook.read_text() == "#!/bin/sh\nexit 0\n"
    hook.unlink()
    assert gate(root, "install").returncode == 0
    assert gate(root, "install").returncode == 0        # running it twice is fine


def test_show_prints_the_limit_and_the_current_size(repo: Path) -> None:
    wt = worktree(repo, "t1")
    assert "no limit" in gate(wt, "show").stdout
    gate(wt, "set", "--lines", "10", "--files", "4")
    write_lines(wt / "a.txt", 3)
    git(wt, "add", "-A")
    shown = gate(wt, "show").stdout
    assert "limit: 10 lines, 4 files against main" in shown and "now:   3 lines, 1 files" in shown
