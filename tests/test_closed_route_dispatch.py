"""Staging does not claim reopening and only reads visible current marks."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "closed_route_dispatch", raising=False)
    return importlib.import_module("closed_route_dispatch")


def case():
    future = SimpleNamespace(slot_id=0, source_node=0, nodes=(0, 7, 3, 2), edges=(0, 1, 2),
                             chokepoints=(7, 3), chokepoint_positions=(1, 0))
    immediate = SimpleNamespace(slot_id=1, source_node=7, nodes=(7, 3, 2), edges=(1, 2),
                                chokepoints=(7, 3), chokepoint_positions=(1, 0))
    network = SimpleNamespace(routes=(future, immediate), edge_transit_weeks=(2, 0, 1))
    obs = {"graph_now.open": np.zeros(2), "graph_now.open.observed": np.ones(2),
           "graph_now.tau": np.asarray([2., 0., 1.]), "graph_now.tau.observed": np.ones(3)}
    return network, obs


def test_default_does_not_read_config_or_observation(module):
    assert module.ClosedRouteDispatch(None).adjustments({}) == {}


def test_future_choke_stages_but_source_choke_and_zero_lead_do_not(module):
    network, obs = case()
    assert module.ClosedRouteDispatch(network, floor=0.25).adjustments(obs) == {(0, 1): 0.25, (0, 0): 0.25}


def test_open_routes_are_unchanged_and_hidden_marks_are_not_read(module):
    network, obs = case()
    obs["graph_now.open"][0] = 1
    obs["graph_now.open.observed"][1] = 0
    obs["graph_now.open"][1] = np.nan
    assert module.ClosedRouteDispatch(network, floor=0.25).adjustments(obs) == {}


def test_hidden_transit_uses_only_public_nominal_and_longer_minimum_disables(module):
    network, obs = case()
    obs["graph_now.tau.observed"][:] = 0
    obs["graph_now.tau"][:] = np.nan
    policy = module.ClosedRouteDispatch(network, floor=0.5)
    assert policy.adjustments(obs) == {(0, 1): 0.5, (0, 0): 0.5}
    assert module.ClosedRouteDispatch(network, floor=0.5, minimum_lead=3).adjustments(obs) == {}


@pytest.mark.parametrize("value", [True, -1, 1.1, float("nan"), float("inf"), "0.5"])
def test_invalid_floor_is_rejected(module, value):
    with pytest.raises(ValueError, match="closed_route_floor"):
        module.ClosedRouteDispatch(None, floor=value)


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1"])
def test_invalid_lead_is_rejected(module, value):
    with pytest.raises(ValueError, match="closed_route_min_lead"):
        module.ClosedRouteDispatch(None, minimum_lead=value)
