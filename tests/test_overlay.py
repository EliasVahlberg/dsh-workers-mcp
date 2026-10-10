"""The shipped tool-policy overlay: the template, its renderer, and the plugin's own test."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dsh_workers_mcp import dshctl

OVERLAY = dshctl.OVERLAY_DIR
TEMPLATE = (OVERLAY / "policy.patch.yml").read_text()


def test_the_overlay_ships_in_the_package_with_everything_dsh_needs() -> None:
    for name in ("index.ts", "package.json", "policy.patch.yml", "README.md", "test.ts"):
        assert (OVERLAY / name).is_file(), name
    assert '"type": "module"' in (OVERLAY / "package.json").read_text()   # load-bearing


def test_the_template_defines_every_preset_and_a_fail_closed_fallback() -> None:
    for preset in ("read-only", "supervised", "autonomous", "danger-full-access"):
        assert re.search(rf"^      {preset}:", TEMPLATE, re.MULTILINE), preset   # in the table
        assert re.search(rf"^          {preset}:", TEMPLATE, re.MULTILINE), preset   # in the rules
    assert "defaultPreset: supervised" in TEMPLATE
    assert re.search(r"^        fallback:", TEMPLATE, re.MULTILINE)
    assert "failing closed" in TEMPLATE


def test_the_template_carries_nothing_personal_or_project_specific() -> None:
    for text in (TEMPLATE, (OVERLAY / "README.md").read_text(), (OVERLAY / "index.ts").read_text()):
        assert "/home/" not in text and "~/system" not in text
        assert not re.search(r"\b(elias|necesse|arcane|saltglass|deepseek-harness-eval)\b",
                             text, re.IGNORECASE)
        assert "heredoc" not in text.lower()


def test_rendering_fills_in_an_absolute_path_that_exists() -> None:
    rendered = dshctl.render_policy_patch()
    assert "@PLUGIN_DIR@" not in rendered
    match = re.search(r"name: '([^']+)'", rendered)
    assert match and Path(match.group(1)).is_absolute() and Path(match.group(1)).is_file()
    assert match.group(1).endswith("/overlays/tool-policy/index.ts")


def test_a_path_with_a_quote_stays_valid_yaml(monkeypatch, tmp_path) -> None:
    odd = tmp_path / "it's here"
    odd.mkdir()
    (odd / "policy.patch.yml").write_text("name: '@PLUGIN_DIR@/index.ts'\n")
    monkeypatch.setattr(dshctl, "OVERLAY_DIR", odd)
    assert dshctl.render_policy_patch() == f"name: '{str(odd).replace(chr(39), chr(39) * 2)}/index.ts'\n"


def test_policy_patch_writes_a_file_and_will_not_overwrite_it_silently(tmp_path, capsys) -> None:
    out = tmp_path / "sub" / "tool-policy.patch.yml"
    assert dshctl.main(["policy-patch", "--out", str(out)]) == 0
    assert out.read_text() == dshctl.render_policy_patch()
    assert "--dump-config" in capsys.readouterr().err
    assert dshctl.main(["policy-patch", "--out", str(out)]) == 1            # exists: refuse
    out.write_text("mine")
    assert dshctl.main(["policy-patch", "--out", str(out), "--force"]) == 0
    assert out.read_text() == dshctl.render_policy_patch()


def test_policy_patch_prints_to_stdout_by_default(capsys) -> None:
    assert dshctl.main(["policy-patch"]) == 0
    assert capsys.readouterr().out == dshctl.render_policy_patch()


def test_the_template_and_the_plugin_test_use_the_same_push_pattern() -> None:
    """test.ts checks the regex by value; keep it the one the patch ships."""
    test_ts = (OVERLAY / "test.ts").read_text()
    shipped = re.search(r"pattern: '(\\bgit\\b[^']+)'", TEMPLATE).group(1)
    tested = re.search(r"const push = '([^']+)'", test_ts).group(1)
    assert shipped.replace("\\", "\\\\") == tested


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_plugin_passes_its_own_rule_and_validation_tests() -> None:
    result = subprocess.run(["node", "test.ts"], cwd=OVERLAY, capture_output=True, text=True,
                            timeout=60)
    if "ERR_UNKNOWN_FILE_EXTENSION" in result.stderr or "Unknown file extension" in result.stderr:
        pytest.skip("this node cannot run TypeScript directly (needs 22.18+)")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "malformed rejected: default must be allow" in result.stdout
    assert "FAIL" not in result.stdout
