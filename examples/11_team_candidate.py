"""Freeze a candidate and its baseline before running Linux checks/comparisons."""

import hashlib
import json
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import fire
from shockbench_flow_agent.submission import build_submission

from sbf_starter.agents import resolve


ROOT = Path(__file__).resolve().parents[1]


def _freeze(source, target):
    with tempfile.TemporaryDirectory(prefix="sbf-freeze-") as tmp:
        archive = Path(build_submission(source, Path(tmp) / "source.zip"))
        with ZipFile(archive) as packed:
            packed.extractall(target)


def _fingerprint(folder):
    with tempfile.TemporaryDirectory(prefix="sbf-fingerprint-") as tmp:
        archive = Path(build_submission(folder, Path(tmp) / "agent.zip"))
        return hashlib.sha256(archive.read_bytes()).hexdigest()


def _git_metadata():
    def read(*args):
        try:
            result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
        except OSError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    return {
        "head_commit": read("rev-parse", "HEAD"),
        "branch": read("branch", "--show-current"),
        "working_tree_status": read("status", "--porcelain"),
        "upstream_commits": {
            branch: read("rev-parse", f"origin/{branch}")
            for branch in ("main", "integration", "analytics", "network-delivery")
        },
    }


def main(
    preset="forecast_bom",
    source="team_agent",
    against="baseline",
    queue_eta=True,
    queue_forecast=False,
    shortage_cost_model=None,
    fuel_replenishment=None,
    out=None,
):
    """Write candidate/, baseline/ and a provenance manifest; never promote policy."""
    for name, value in (("queue_eta", queue_eta), ("queue_forecast", queue_forecast)):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
    if shortage_cost_model is not None and not isinstance(shortage_cost_model, bool):
        raise ValueError("shortage_cost_model must be a boolean or None")
    if fuel_replenishment is not None and not isinstance(fuel_replenishment, bool):
        raise ValueError("fuel_replenishment must be a boolean or None")
    source, baseline = resolve(source).resolve(), resolve(against).resolve()
    if not source.is_dir() or not baseline.is_dir() or source == baseline:
        raise ValueError("source and against must be different submission directories")
    presets_path = source / "experiments" / "presets.json"
    presets = json.loads(presets_path.read_text(encoding="utf-8"))
    if preset not in presets or not isinstance(presets[preset], dict):
        raise ValueError(f"unknown planner preset: {preset}")
    options = dict(presets[preset])
    if shortage_cost_model is not None:
        options["shortage_cost_model"] = shortage_cost_model
    if fuel_replenishment is not None:
        options["fuel_replenishment_enabled"] = fuel_replenishment
    before = {"source": _fingerprint(source), "baseline": _fingerprint(baseline)}
    folder = Path(out or f"outputs/11_team_candidate/{datetime.now():%Y-%m-%d_%H-%M-%S_%f}").resolve()
    if folder.exists():
        raise FileExistsError("choose a new output folder; frozen candidates are never overwritten")
    if source in folder.parents or baseline in folder.parents:
        raise ValueError("output must not be inside either source submission")
    folder.mkdir(parents=True)
    candidate_folder, baseline_folder = folder / "candidate", folder / "baseline"
    _freeze(source, candidate_folder)
    _freeze(baseline, baseline_folder)
    params_path = candidate_folder / "params.json"
    params = json.loads(params_path.read_text(encoding="utf-8")) if params_path.exists() else {}
    params.update(
        allocation_enabled=True,
        queue_eta_enabled=queue_eta,
        queue_forecast_enabled=queue_forecast,
        planner_options=options,
    )
    params_path.write_text(json.dumps(params, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    after = {"source": _fingerprint(source), "baseline": _fingerprint(baseline)}
    if before != after:
        raise RuntimeError("source files changed while freezing; discard this run and use a new output folder")
    frozen = {"candidate": _fingerprint(candidate_folder), "baseline": _fingerprint(baseline_folder)}
    if frozen["baseline"] != before["baseline"]:
        raise RuntimeError("frozen baseline is not identical to its source")
    manifest = {
        "status": "frozen_not_evaluated",
        "preset": preset,
        "params": params,
        "source_directories": {"candidate": str(source), "baseline": str(baseline)},
        "source_submission_sha256": before,
        "submission_sha256": frozen,
        "git": _git_metadata(),
        "evaluation": None,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen run: {folder}")
    print(f"Candidate: {candidate_folder}")
    print(f"Baseline: {baseline_folder}")
    print("No tests, scoring, upload or policy promotion were performed.")


if __name__ == "__main__":
    fire.Fire(main)
