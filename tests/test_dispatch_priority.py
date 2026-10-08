"""Optional priorities never turn hidden forecast padding into zero demand."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def policy_type(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "dispatch_priority", raising=False)
    return importlib.import_module("dispatch_priority").DispatchPriority


def case():
    profiles = [
        {"id": "osat", "region": "A", "osat": {"packages": {"raw": "chip"}}},
        {"id": "empty", "region": "B", "sink": {"demand": {
            "chip": {"dbar": 0, "pi": 5, "backlog": False}}}},
        {"id": "consumer", "region": "C", "sink": {"demand": {
            "chip": {"dbar": 10, "pi": 5, "backlog": False},
            "other": {"dbar": 2, "pi": 1, "backlog": False}}}},
    ]
    routes = tuple(
        SimpleNamespace(slot_id=i, source_node=0, destination_node=dest, commodity_id=k)
        for i, (dest, k) in enumerate(((1, 0), (2, 0), (2, 1)))
    )
    network = SimpleNamespace(routes=routes, node_names=("osat", "empty", "consumer"),
                              commodity_names=("chip", "other"), slots_from={(0, 0): (0, 1), (0, 1): (2,)})
    config = {"static": {"instance": {"nodes": profiles}},
              "layout": {"demands": [(1, 0), (2, 0), (2, 1)]}}
    obs = {"demand_forecast.qty": np.asarray([[0., 0.], [10., 10.], [2., 2.]]),
           "demand_forecast.qty.observed": np.ones((3, 2), dtype=np.int8),
           "backlog.qty": np.zeros(3), "backlog.qty.observed": np.ones(3, dtype=np.int8)}
    return config, network, obs


def test_default_is_baseline_identity(policy_type):
    config, network, obs = case()
    flows = np.ones(3)
    assert policy_type(config, network).apply(flows, obs) is flows


def test_known_zero_demand_is_not_shipped(policy_type):
    config, network, obs = case()
    flows = np.asarray([100., 100., 100.])
    out = policy_type(config, network, skip_zero_demand=True).apply(flows, obs)
    np.testing.assert_array_equal(out, [0, 100, 100])
    np.testing.assert_array_equal(flows, [100, 100, 100])


@pytest.mark.parametrize("hidden", [True, False])
def test_positive_or_hidden_forecast_is_not_called_zero(policy_type, hidden):
    config, network, obs = case()
    if hidden:
        obs["demand_forecast.qty.observed"][0] = 0
        obs["demand_forecast.qty"][0] = [np.nan, 1e99]
    else:
        obs["demand_forecast.qty"][0, 1] = 1
    flows = np.ones(3)
    np.testing.assert_array_equal(flows, policy_type(config, network, skip_zero_demand=True).apply(flows, obs))


@pytest.mark.parametrize("hidden", [True, False])
def test_backlog_cannot_be_discarded(policy_type, hidden):
    config, network, obs = case()
    config["static"]["instance"]["nodes"][1]["sink"]["demand"]["chip"]["backlog"] = True
    obs["backlog.qty"][0] = 1
    if hidden:
        obs["backlog.qty.observed"][0] = 0
    flows = np.ones(3)
    np.testing.assert_array_equal(flows, policy_type(config, network, skip_zero_demand=True).apply(flows, obs))


def test_product_priority_preserves_zero_masks(policy_type):
    config, network, obs = case()
    flows = np.asarray([0., 100., 100.])
    out = policy_type(config, network, product_priority_power=1).apply(flows, obs)
    np.testing.assert_array_equal(out, [0, 100, 20])


def test_hidden_future_padding_does_not_affect_zero_filter(policy_type):
    config, network, obs = case()
    obs["demand_forecast.qty.observed"][0, 1] = 0
    obs["demand_forecast.qty"][0, 1] = np.nan
    assert policy_type(config, network, skip_zero_demand=True).apply(np.ones(3), obs)[0] == 0


@pytest.mark.parametrize("name", ["product_priority_power", "sink_demand_power"])
@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
def test_invalid_power_is_rejected(policy_type, name, value):
    config, network, _ = case()
    with pytest.raises(ValueError, match=name):
        policy_type(config, network, **{name: value})


def test_invalid_boolean_flag_is_rejected(policy_type):
    config, network, _ = case()
    with pytest.raises(ValueError, match="boolean"):
        policy_type(config, network, skip_zero_demand="False")
