"""Candidate preparation stays separate from production policy and baseline."""

import importlib.util
import json
import sys

import gymnasium as gym
import pytest
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load
from tests.conftest import ROOT


@pytest.fixture
def preparation():
    spec = importlib.util.spec_from_file_location("team_candidate_example", ROOT / "examples/11_team_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_freeze_preserves_baseline_and_wires_real_modules(preparation, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    source, baseline = ROOT / "agents/team_agent", ROOT / "agents/baseline"
    before = preparation._fingerprint(source), preparation._fingerprint(baseline)
    output = tmp_path / "frozen"
    preparation.main(out=str(output), preset="baseline_conservative", queue_eta=False, shortage_cost_model=False)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "frozen_not_evaluated"
    assert manifest["evaluation"] is None
    assert manifest["submission_sha256"]["baseline"] == before[1]
    assert (preparation._fingerprint(source), preparation._fingerprint(baseline)) == before
    env = gym.make(env_id("tiny"), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        candidate = load(output / "candidate")(config)
        assert candidate.pipeline is not None
        assert candidate.pipeline.need_planner.production_horizon == 1
        assert candidate.pipeline.need_planner.production_enabled is False
        assert candidate.pipeline.need_planner.shortage_cost_model is False
        assert candidate.act(obs)["flows"].shape == tuple(config["spaces"]["action"]["flows"]["shape"])
        assert candidate.last_allocation is not None
        assert load(source)(config).pipeline is None
    finally:
        env.close()


def test_freeze_refuses_existing_output(preparation, tmp_path, monkeypatch):
    monkeypatch.chdir(ROOT)
    with pytest.raises(FileExistsError):
        preparation.main(out=str(tmp_path))
    assert not (tmp_path / "candidate").exists()


def test_freeze_refuses_output_inside_source(preparation, monkeypatch):
    monkeypatch.chdir(ROOT)
    with pytest.raises(ValueError, match="inside"):
        preparation.main(out=str(ROOT / "agents/team_agent/never-create-this-output"))


@pytest.mark.parametrize(
    "options",
    [
        [],
        {"production_horizon": True},
        {"production_horizon": 0},
        {"safety_stock": "False"},
        {"fuel_replenishment_enabled": "False"},
        {"wrong": 1},
    ],
)
def test_factory_rejects_malformed_planner_settings(options):
    load(ROOT / "agents/team_agent")
    with pytest.raises(ValueError):
        sys.modules["integration"].validate_planner_options(options)


def test_factory_does_not_enable_policy_implicitly():
    load(ROOT / "agents/team_agent")
    assert sys.modules["integration"].build_pipeline(None, None) is None


@pytest.mark.parametrize("queue_eta,queue_forecast", [("False", False), (False, "False")])
def test_preparation_rejects_string_flags(preparation, queue_eta, queue_forecast):
    with pytest.raises(ValueError, match="boolean"):
        preparation.main(queue_eta=queue_eta, queue_forecast=queue_forecast)


@pytest.mark.parametrize("enabled", [False, True])
def test_frozen_replenishment_toggle_is_explicit(preparation, tmp_path, monkeypatch, enabled):
    monkeypatch.chdir(ROOT)
    output = tmp_path / "frozen"
    preparation.main(out=str(output), fuel_replenishment=enabled)
    params = json.loads((output / "candidate" / "params.json").read_text())
    assert params["planner_options"]["fuel_replenishment_enabled"] is enabled


def test_preparation_rejects_string_replenishment_flag(preparation):
    with pytest.raises(ValueError, match="boolean"):
        preparation.main(fuel_replenishment="False")
