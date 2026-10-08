"""Integrated submission regressions, including full-episode action parity."""

import json
import os
import shutil
from zipfile import ZipFile

import gymnasium as gym
import numpy as np
import pytest
from shockbench_flow_agent.submission import build_submission, check_zip, extract_submission, missing_imports
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load
from tests.conftest import ROOT


TEAM = ROOT / "agents" / "team_agent"
BASELINE = ROOT / "agents" / "baseline"


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_neutral_policy_matches_baseline_every_week(task, tmp_path):
    neutral = tmp_path / "neutral"
    shutil.copytree(TEAM, neutral, ignore=shutil.ignore_patterns("__pycache__"))
    (neutral / "params.json").write_text("{}\n", encoding="utf-8")
    env = gym.make(env_id(task), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        baseline = load(BASELINE)(config)
        team = load(neutral)(config)
        network = team.network
        assert len(network.routes) == env.action_space["flows"].shape[0]
        assert team.through == tuple(tuple(row) for row in baseline.through)
        np.testing.assert_array_equal(team.cap, baseline.cap)
        done = False
        weeks = 0
        while not done:
            action = team.act(obs)
            np.testing.assert_array_equal(action["flows"], baseline.act(obs)["flows"])
            assert team.network is network
            assert np.all(np.isfinite(action["flows"]))
            assert np.all(action["flows"] >= 0)
            obs, _, terminated, truncated, _ = env.step(action)
            done = terminated or truncated
            weeks += 1
        assert weeks == config["T"]
    finally:
        env.close()


@pytest.mark.parametrize("per_slot", [False, True])
def test_params_masks_and_hidden_closures_preserve_baseline(tmp_path, per_slot):
    env = gym.make(env_id("small"), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        count = env.action_space["flows"].shape[0]
        fraction = np.linspace(0.1, 0.9, count).tolist() if per_slot else 0.6
        params = {"fraction": fraction, "closure_power": 1.7}
        folders = []
        for name, source in (("baseline", BASELINE), ("team", TEAM)):
            folder = tmp_path / name
            shutil.copytree(source, folder, ignore=shutil.ignore_patterns("__pycache__"))
            (folder / "params.json").write_text(json.dumps(params), encoding="utf-8")
            folders.append(folder)
        baseline, team = (load(folder)(config) for folder in folders)
        changed = {key: value.copy() for key, value in obs.items()}
        changed["action_mask"][::3] = 0
        changed["graph_now.open"][:] = np.linspace(0, 1, len(team.network.chokepoints))
        changed["graph_now.open.observed"][:] = 1
        changed["graph_now.open.observed"][0] = 0
        np.testing.assert_array_equal(team.act(changed)["flows"], baseline.act(changed)["flows"])
        assert np.all(team.act(changed)["flows"][::3] == 0)
    finally:
        env.close()


def test_packed_submission_loads_its_own_network(tmp_path):
    archive = build_submission(TEAM, tmp_path / "team.zip")
    checked = check_zip(archive)
    assert {"agent.py", "network.py", "contracts.py", "integration.py"}.issubset(name for name, _ in checked.files)
    root = tmp_path / "unpacked"
    # check_zip has validated every member; portable extraction tests sibling imports.
    with ZipFile(archive) as zipped:
        zipped.extractall(root)
    files = [str(path.relative_to(root).as_posix()) for path in root.rglob("*.py")]
    for file in files:
        assert missing_imports((root / file).read_bytes(), files) == []
    env = gym.make(env_id("small"), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        team = load(root)(config)
        assert len(team.network.routes) == env.action_space["flows"].shape[0]
        assert team.act(obs)["flows"].shape == env.action_space["flows"].shape
    finally:
        env.close()


@pytest.mark.skipif(not hasattr(os, "fchmod"), reason="server extraction needs os.fchmod; verify in Ubuntu/WSL")
def test_server_extractor_preserves_submission_helpers(tmp_path):
    archive = build_submission(TEAM, tmp_path / "server.zip")
    root = extract_submission(archive, tmp_path / "server").root
    env = gym.make(env_id("small"), entropy=12345)
    try:
        observation, info = env.reset(seed=0, options={"episode": 0})
        agent = load(root)(agent_config_from_reset(env, observation, info))
        assert len(agent.network.routes) == env.action_space["flows"].shape[0]
        assert agent.act(observation)["flows"].shape == env.action_space["flows"].shape
    finally:
        env.close()
