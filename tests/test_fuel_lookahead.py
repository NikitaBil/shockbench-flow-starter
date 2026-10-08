"""Conditional local search preserves current action and observation contracts."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "fuel_lookahead", raising=False)
    return importlib.import_module("fuel_lookahead")


def case():
    goods = ("lng", "crude", "wafer", "chip_raw", "chip")
    routes = tuple(SimpleNamespace(slot_id=k, source_node=0, destination_node=1, commodity_id=k,
                                   edge_id=k, chokepoints=(), nominal_transit_weeks=0) for k in (0, 1))
    network = SimpleNamespace(node_names=("terminal", "grid", "fab", "sink"), commodity_names=goods,
                              routes=routes, edge_head=(1, 1), chokepoints=())
    fuel_stock = {name: {"storage": 1000} for name in goods[:2]}
    config = {"T": 20, "layout": {"fabs": [2], "grids": [1],
                                  "stock_slots": [(0, 0), (0, 1), (1, 0), (1, 1), (2, 2)]},
              "static": {"instance": {"nodes": [
                  {"id": "terminal", "stock": fuel_stock},
                  {"id": "grid", "stock": fuel_stock,
                   "grid": {"priority": "base_first", "shares": {"lng": 0.4, "crude": 0.1, "unmodelled": 0.5},
                            "voll": 1}},
                  {"id": "fab", "fab": {"grid": "grid", "input": "wafer", "product": "chip_raw",
                                           "cap0": 20, "e": 2}},
                  {"id": "sink", "sink": {"demand": {"chip": {"pi": 2000}}}},
              ]}}}
    batch = SimpleNamespace(enabled=True, groups={(1, 0): [0], (1, 1): [1]}, latest_week={}, complete_pulse=True,
                            grids={(1, 0): (0, 100., 0.4, 30., 1.), (1, 1): (0, 100., 0.1, 0., 1.)})
    obs = {"week": np.asarray([1]), "stock.qty": np.asarray([30., 10., 0., 0., 10.]),
           "stock.qty.observed": np.ones(5), "pipeline.qty.observed": np.zeros(1),
           "action_mask.observed": np.ones(1), "graph_now.u": np.asarray([100., 100.]),
           "graph_now.u.observed": np.ones(2), "action_mask": np.ones(2)}
    for key, value in (("grid.G_bar", 100.), ("grid.y_bar", 70.), ("fab.R", 1.), ("fab.cap_eff", 20.)):
        obs["graph_now." + key] = np.asarray([value])
        obs["graph_now." + key + ".observed"] = np.ones(1)
    return config, network, batch, obs


def test_default_is_exact_identity_without_reading_config(module):
    flows = np.zeros(2)
    assert module.FuelLookahead({}, None, SimpleNamespace(enabled=False)).apply(flows, None, {}) is flows


def test_lookahead_coordinates_crude_with_next_week_rationed_gas(module):
    config, network, batch, obs = case()
    policy = module.FuelLookahead(config, network, batch, horizon=2)
    controls, requests = np.asarray([30., 10.]), np.asarray([30., 10.])
    result = policy.apply(controls, requests, obs)
    np.testing.assert_allclose(result, [30, 0])
    assert policy.last_evaluations > 0
    np.testing.assert_array_equal(controls, [30, 10])
    np.testing.assert_array_equal(obs["stock.qty"], [30, 10, 0, 0, 10])
    repeated = policy.apply(controls, requests, obs)
    np.testing.assert_array_equal(repeated, result)


def test_no_industry_value_favors_earlier_base_load_service(module):
    config, network, batch, obs = case()
    policy = module.FuelLookahead(config, network, batch, horizon=2, industry_weight=0)
    assert policy.apply(np.asarray([30., 10.]), np.asarray([30., 10.]), obs)[1] == 10


def test_unobserved_local_stock_or_mask_keeps_control_without_padding(module):
    config, network, batch, obs = case()
    policy = module.FuelLookahead(config, network, batch, horizon=2)
    control, requests = np.asarray([0., 10.]), np.asarray([30., 10.])
    obs["stock.qty.observed"][2] = 0
    obs["stock.qty"][2] = np.nan
    np.testing.assert_array_equal(policy.apply(control, requests, obs), control)
    obs["action_mask.observed"][:] = 0
    assert policy.apply(control, requests, obs) is control


def test_rejected_zero_route_cannot_be_reactivated(module):
    config, network, batch, obs = case()
    policy = module.FuelLookahead(config, network, batch, horizon=3)
    result = policy.apply(np.asarray([0., 10.]), np.asarray([0., 10.]), obs)
    assert result[0] == 0
    assert 0 <= result[1] <= 10


@pytest.mark.parametrize("gas_flow", [0.0, 10.0, 30.0])
def test_preserve_rationed_keeps_first_gas_action_exactly(module, gas_flow):
    config, network, batch, obs = case()
    policy = module.FuelLookahead(config, network, batch, horizon=4, preserve_rationed=True)
    flows, requests = np.asarray([gas_flow, 10.]), np.asarray([30., 10.])
    result = policy.apply(flows, requests, obs)
    assert result[0] == gas_flow
    assert 0 <= result[1] <= requests[1]


def test_non_boolean_preserve_rationed_is_rejected(module):
    with pytest.raises(ValueError, match="preserve_rationed"):
        module.FuelLookahead({}, None, None, preserve_rationed=1)


@pytest.mark.parametrize("value", [True, -1, 7, 2.5, "2"])
def test_bad_horizon_is_rejected(module, value):
    with pytest.raises(ValueError, match="horizon"):
        module.FuelLookahead({}, None, None, horizon=value)
