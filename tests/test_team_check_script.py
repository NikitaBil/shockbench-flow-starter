"""Bash orchestration tests only; mocked uv calls are not scoring evidence."""

import os
import shutil
import subprocess

import pytest

from tests.conftest import ROOT


BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(os.name == "nt" or BASH is None, reason="orchestration tests require Unix Bash")

MOCK_COMMAND = r"""
function uname() { printf 'Linux\n'; }
function uv() {
    printf '%s\n' "$*" >> "$MOCK_LOG"
    if [[ "${3:-}" == 'compare' && "$*" == *'--task=full'* && "${FAIL_FULL:-0}" == '1' ]]; then
        return 23
    fi
    for argument in "$@"; do
        if [[ "$argument" == --out=* ]]; then
            printf '{"test_double": true}\n' > "${argument#--out=}"
        fi
    done
}
export -f uname uv
bash scripts/check_team_integration.sh "$1"
"""


def run_script(tmp_path, *, existing=False, fail_full=False):
    out = tmp_path / "mock-run"
    if existing:
        out.mkdir()
    log = tmp_path / "mock-commands.txt"
    env = os.environ | {
        "MOCK_LOG": log.as_posix(),
        "EPISODES": "2",
        "ENTROPY": "67890",
        "FAIL_FULL": "1" if fail_full else "0",
    }
    result = subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", MOCK_COMMAND, "mock-check", out.as_posix()],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result, out, log


def test_linux_script_snapshots_checks_and_compares_both_tasks(tmp_path):
    result, out, log = run_script(tmp_path)
    assert result.returncode == 0, result.stderr
    commands = log.read_text().splitlines()
    assert "pytest" in commands[0]
    assert "sbf pack team_agent" in commands[1]
    assert "sbf pack baseline" in commands[2]
    for task in ("small", "full"):
        assert any("sbf check" in row and f"--task={task}" in row for row in commands)
        comparisons = [row for row in commands if "sbf compare" in row and f"--task={task}" in row]
        assert len(comparisons) == 1
        assert "--episodes=2" in comparisons[0] and "--entropy=67890" in comparisons[0]
        assert "--cpu_budget=True" in comparisons[0]
        assert "candidate.zip" in comparisons[0] and "baseline.zip" in comparisons[0]
        assert (out / f"compare_{task}.json").is_file()
    assert (out / "sha256.txt").is_file()
    assert not any("upload" in row for row in commands)


def test_linux_script_does_not_overwrite_existing_run(tmp_path):
    result, _, log = run_script(tmp_path, existing=True)
    assert result.returncode != 0
    assert "Output already exists" in result.stderr
    assert not log.exists()


def test_linux_script_propagates_runner_failure(tmp_path):
    result, _, _ = run_script(tmp_path, fail_full=True)
    assert result.returncode == 23
    assert "Completed." not in result.stdout
