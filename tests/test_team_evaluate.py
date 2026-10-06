"""The comparison recorder must preserve evidence and failures."""

import json
from importlib import import_module
from types import SimpleNamespace

import pytest

from tests.conftest import ROOT


evaluate = import_module("examples.09_team_evaluate")


@pytest.fixture(autouse=True)
def supported_runner(monkeypatch):
    monkeypatch.setattr(evaluate.platform, "system", lambda: "Linux")


@pytest.mark.parametrize(
    ("diff", "interval", "conclusion"),
    [
        (0.05, (0.01, 0.09), "better"),
        (-0.05, (-0.09, -0.01), "worse"),
        (0.02, (-0.01, 0.05), "insufficient_evidence"),
        (0.0, (0.0, 0.0), "equal_on_evaluated_episodes"),
    ],
)
def test_recorder_saves_comparison_and_fingerprints(tmp_path, monkeypatch, diff, interval, conclusion):
    result = SimpleNamespace(diff=diff, interval=interval)
    calls = []

    def compare(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(evaluate.scoring, "compare", compare)
    monkeypatch.setattr(evaluate.scoring, "as_dict", lambda _: {"difference": diff, "interval": interval})
    out = tmp_path / "run"
    evaluate.main(quick=True, episodes=4, out=str(out))
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "completed"
    assert saved["conclusion"] == conclusion
    assert saved["scoring_mode"] == "quick_smoke"
    assert saved["comparison"]["difference"] == diff
    assert saved["settings"]["entropy"] == 67890
    assert len(saved["submission_sha256"]["candidate"]) == 64
    assert len(saved["submission_sha256"]["baseline"]) == 64
    assert saved["submission_sha256"]["candidate"] != saved["submission_sha256"]["baseline"]
    assert calls[0][0][2:] == ("small", 4)
    assert calls[0][1]["quick"] is True
    assert (out / "report.txt").is_file()


def test_recorder_keeps_failure_evidence(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("reference computation failed")

    monkeypatch.setattr(evaluate.scoring, "compare", failed)
    out = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="reference computation failed"):
        evaluate.main(out=str(out))
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["error"]["type"] == "RuntimeError"
    assert saved["comparison"] is None
    assert "elapsed_seconds" in saved


def test_same_submission_is_not_accepted_as_its_own_baseline(tmp_path):
    with pytest.raises(ValueError, match="different submission folders"):
        evaluate.main(agent="team_agent", against="team_agent", out=str(tmp_path))


def test_existing_result_is_not_overwritten(tmp_path):
    result = tmp_path / "result.json"
    result.write_text('{"status": "old"}', encoding="utf-8")
    with pytest.raises(FileExistsError, match="new output folder"):
        evaluate.main(out=str(tmp_path))
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "old"


def test_changed_source_invalidates_recorded_result(tmp_path, monkeypatch):
    import shutil

    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT / "agents" / "team_agent", candidate, ignore=shutil.ignore_patterns("__pycache__"))

    def changing(*args, **kwargs):
        source = candidate / "agent.py"
        source.write_text(source.read_text(encoding="utf-8") + "\n# Changed during the run.\n", encoding="utf-8")
        return SimpleNamespace(diff=0.0, interval=(0.0, 0.0))

    monkeypatch.setattr(evaluate.scoring, "compare", changing)
    monkeypatch.setattr(evaluate.scoring, "as_dict", lambda _: {"difference": 0.0})
    out = tmp_path / "changed"
    with pytest.raises(RuntimeError, match="changed during evaluation"):
        evaluate.main(agent=str(candidate), out=str(out))
    saved = json.loads((out / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["comparison"] is None


def test_windows_score_fails_early_with_actionable_message(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate.platform, "system", lambda: "Windows")
    with pytest.raises(RuntimeError, match="Ubuntu/WSL"):
        evaluate.main(out=str(tmp_path))
    saved = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert saved["comparison"] is None


def test_parity_evidence_is_not_presented_as_rss(tmp_path, monkeypatch):
    evidence = {"identical_actions": True, "episodes": [{"weeks": 52, "matching_action_weeks": 52}]}
    monkeypatch.setattr(evaluate, "replay_parity", lambda *args: evidence)
    evaluate.main(mode="parity", episodes=1, out=str(tmp_path))
    saved = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert saved["scoring_mode"] == "action_parity"
    assert saved["comparison"] is None
    assert saved["parity"] == evidence
    assert saved["conclusion"] == "identical_actions_on_replayed_observations"
