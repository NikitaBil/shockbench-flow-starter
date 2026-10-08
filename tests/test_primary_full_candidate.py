"""The primary policy must preserve the verified frozen candidate's decisions."""

import json

import gymnasium as gym
import numpy as np
import pytest
from shockbench_flow_agent.submission import build_submission, check_zip
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load, resolve
from tests.conftest import ROOT


PRIMARY = ROOT / "agents/team_agent"
FROZEN = ROOT / "agents/team_agent_0781480"
FROZEN_SHA = "cc7c0cf6712cd16f7b0c538b3930fa6b96f8055d9aebb287dd2105b3576e1d51"


def test_primary_uses_the_verified_parameters():
    assert resolve("team_agent").resolve() == PRIMARY.resolve()
    assert resolve("team_agent_0781480").resolve() == FROZEN.resolve()
    assert json.loads((PRIMARY / "params.json").read_text(encoding="utf-8")) == json.loads(
        (FROZEN / "params.json").read_text(encoding="utf-8")
    )


def test_frozen_copy_keeps_its_exact_submission_hash(tmp_path):
    checked = check_zip(build_submission(FROZEN, tmp_path / "frozen.zip"))
    assert checked.sha256 == FROZEN_SHA


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
@pytest.mark.parametrize("entropy", [0, 20261013])
def test_primary_matches_frozen_every_week(task, entropy):
    primary_class, frozen_class = load(PRIMARY), load(FROZEN)
    env = gym.make(env_id(task), entropy=entropy)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        primary, frozen = primary_class(config), frozen_class(config)
        assert primary.pipeline is None and frozen.pipeline is None
        weeks, done = 0, False
        while not done:
            before = {key: value.copy() for key, value in obs.items()}
            action, expected = primary.act(obs), frozen.act(obs)
            assert action.keys() == expected.keys()
            for key in action:
                np.testing.assert_array_equal(action[key], expected[key])
                assert env.action_space[key].contains(action[key])
            for key in obs:
                np.testing.assert_array_equal(obs[key], before[key])
            obs, _, terminated, truncated, _ = env.step(action)
            weeks += 1
            done = terminated or truncated
        assert weeks == config["T"]
    finally:
        env.close()
