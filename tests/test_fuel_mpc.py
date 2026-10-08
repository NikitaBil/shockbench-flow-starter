"""Fuel forecasts stay opt-in and obey stock-before-arrival dispatch order."""

import importlib
import sys

import gymnasium as gym
import numpy as np
import pytest
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from tests.conftest import ROOT


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "fuel_mpc", raising=False)
    return importlib.import_module("fuel_mpc")


def test_disabled_plan_is_exact_identity(module):
    flows = np.ones(3)
    assert module.FuelMPC(None, None).apply(flows, {}) is flows


@pytest.mark.parametrize("value", [-1, True, 25, 1.5, "12"])
def test_invalid_horizon_rejected(module, value):
    with pytest.raises(ValueError, match="horizon"):
        module.FuelMPC(None, None, horizon=value)


@pytest.mark.parametrize("value", [-1, True, float("nan"), float("inf"), "1"])
def test_invalid_bonus_rejected(module, value):
    with pytest.raises(ValueError, match="industry_bonus"):
        module.FuelMPC(None, None, industry_bonus=value)


def test_first_plan_respects_visible_source_stock_and_mask(module):
    from network import StaticNetwork

    env = gym.make(env_id("tiny"), entropy=12345)
    try:
        observation, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, observation, info)
        network = StaticNetwork(config)
        policy = module.FuelMPC(config, network, horizon=12)
        original = np.zeros(len(network.routes))
        result = policy.apply(original, observation)
        assert policy.last_status == "planned_current_marks_persist"
        assert policy.last_plan["forecast_horizon"] == 12
        assert np.isfinite(result).all() and np.all(result >= 0)
        assert np.all(result[observation["action_mask"] == 0] == 0)
        for pair, slots in network.slots_from.items():
            if pair in policy.stock_rows:
                available = observation["stock.qty"][policy.stock_rows[pair]]
                assert result[list(slots)].sum() <= available + 1e-7
        np.testing.assert_array_equal(original, np.zeros(len(network.routes)))
    finally:
        env.close()


def test_hidden_stock_keeps_control_explicitly_without_reading_padding(module):
    from network import StaticNetwork

    env = gym.make(env_id("tiny"), entropy=12345)
    try:
        observation, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, observation, info)
        network = StaticNetwork(config)
        policy = module.FuelMPC(config, network, horizon=12)
        row = policy.stock_rows[policy.pairs[0]]
        observation["stock.qty.observed"][row] = 0
        observation["stock.qty"][row] = np.nan
        control = np.ones(len(network.routes))
        assert policy.apply(control, observation) is control
        assert policy.last_status == "unobserved_stock_keep_control"
        assert policy.last_plan is None
    finally:
        env.close()
