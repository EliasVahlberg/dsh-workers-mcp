"""scripts/dsh-sandbox.sh: its self-test runs, with a throwaway HOME so no real home is touched."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dsh-sandbox.sh"


def _bwrap_usable() -> bool:
    if shutil.which("bwrap") is None:
        return False
    probe = subprocess.run(["bwrap", "--ro-bind", "/", "/", "true"], capture_output=True)
    return probe.returncode == 0           # fails where user namespaces are disabled


needs_bwrap = pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap is not usable here")


def test_the_script_is_executable_and_documents_itself() -> None:
    assert SCRIPT.is_file() and SCRIPT.stat().st_mode & 0o111
    text = SCRIPT.read_text()
    for option in ("--rw", "--ro", "--env", "--set", "--path", "--cwd", "--no-net", "--self-test"):
        assert option in text


@needs_bwrap
def test_the_self_test_passes_in_a_throwaway_home(tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run([str(SCRIPT), "--self-test"], capture_output=True, text=True,
                            timeout=120, env={"HOME": str(home), "PATH": "/usr/local/bin:/usr/bin:/bin",
                                              "DSH_SANDBOX_SKIP_NET": "1"})   # DNS can take 10 s
    assert result.returncode == 0, result.stdout + result.stderr
    assert "self-test: all passed" in result.stdout
    assert "SKIP  network checks" in result.stdout
    assert "FAIL" not in result.stdout
    assert list(home.iterdir()) == []            # the test cleans up after itself


@needs_bwrap
def test_a_named_variable_passes_and_an_unnamed_one_does_not(tmp_path) -> None:
    env = {"HOME": str(tmp_path), "PATH": "/usr/local/bin:/usr/bin:/bin",
           "PASSED": "yes", "WITHHELD": "no"}
    result = subprocess.run([str(SCRIPT), "--env", "PASSED", "--", "sh", "-c",
                             'echo "passed=${PASSED:-} withheld=${WITHHELD:-}"'],
                            capture_output=True, text=True, timeout=60, env=env)
    assert result.stdout.strip() == "passed=yes withheld="


def test_a_missing_variable_or_command_is_refused(tmp_path) -> None:
    env = {"HOME": str(tmp_path), "PATH": "/usr/local/bin:/usr/bin:/bin"}
    for argv in (["--env", "NOT_SET_ANYWHERE", "--", "true"], []):
        result = subprocess.run([str(SCRIPT), *argv], capture_output=True, text=True, env=env)
        assert result.returncode == 2
