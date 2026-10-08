"""Nominal production distances preserve unknown paths and baseline defaults."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def policy_type(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "production_horizon", raising=False)
    return importlib.import_module("production_horizon").ProductionHorizon


def case():
    routes = tuple(SimpleNamespace(slot_id=i, source_node=i, destination_node=i + 1,
                                   commodity_id=i, nominal_transit_weeks=0 if i == 0 else 1)
                   for i in range(3))
    network = SimpleNamespace(routes=routes, node_names=("source", "fab", "osat", "sink"),
                              commodity_names=("wafer", "raw", "packed"))
    nodes = [{"id": "source"}, {"id": "fab", "fab": {"input": "wafer", "product": "raw", "tau": 6}},
             {"id": "osat", "osat": {"packages": {"raw": "packed"}, "tau": 2}}, {"id": "sink"}]
    config = {"T": 20, "static": {"instance": {"nodes": nodes}},
              "layout": {"stock_slots": [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2), (3, 2)],
                         "demands": [(3, 2)]}}
    return config, network


def test_disabled_is_identity_without_reading_config_or_observations(policy_type):
    flows = np.ones(3)
    assert policy_type({}, None).apply(flows, {}) is flows


def test_recipe_and_zero_transit_edges_are_included_without_claiming_queue_eta(policy_type):
    config, network = case()
    policy = policy_type(config, network, enabled=True)
    assert dict(policy.cutoffs) == {0: 10, 1: 4}
    flows = np.full(3, 10.)
    np.testing.assert_array_equal(policy.apply(flows, {"week": [10]}), flows)
    np.testing.assert_array_equal(policy.apply(flows, {"week": [11]}), [0, 10, 10])
    np.testing.assert_array_equal(policy.apply(flows, {"week": [17]}), [0, 0, 10])
    assert policy.last_removed == (0, 1)
    np.testing.assert_array_equal(flows, [10, 10, 10])


def test_unrepresented_or_unknown_chain_is_not_assigned_a_finite_eta(policy_type):
    config, network = case()
    network.routes = network.routes[:1] + network.routes[2:]
    policy = policy_type(config, network, enabled=True)
    assert not np.isfinite(dict(policy.cutoffs)[0])
    flows = np.full(3, 10.)
    assert policy.apply(flows, {"week": [20]}) is flows


def test_parallel_routes_use_minimum_not_sum_of_nominal_delays(policy_type):
    config, network = case()
    network.routes += (SimpleNamespace(slot_id=3, source_node=1, destination_node=2,
                                       commodity_id=1, nominal_transit_weeks=5),)
    policy = policy_type(config, network, enabled=True)
    assert dict(policy.cutoffs) == {0: 10, 1: 4, 3: 8}
    flows = np.asarray([0., 10., 10., 10.])
    np.testing.assert_array_equal(policy.apply(flows, {"week": [15]}), [0, 10, 10, 0])


@pytest.mark.parametrize("value", [1, "True", None])
def test_non_boolean_flags_are_rejected(policy_type, value):
    with pytest.raises(ValueError, match="production_horizon_enabled"):
        policy_type({}, None, enabled=value)
