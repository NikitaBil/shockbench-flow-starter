"""Fuel batching must not invent stock, consume padding or change other flows."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "fuel_batch", raising=False)
    return importlib.import_module("fuel_batch")


def case():
    routes = tuple(SimpleNamespace(slot_id=k, source_node=0, destination_node=1,
                                   commodity_id=k, chokepoints=(), nominal_transit_weeks=0) for k in (0, 1))
    network = SimpleNamespace(node_names=("terminal", "grid", "fab"), commodity_names=("lng", "crude"),
                              routes=routes, edge_head=(1,))
    config = {"layout": {"stock_slots": [(0, 0), (0, 1), (1, 0), (1, 1)], "grids": [1]},
              "static": {"instance": {"params": {"psi": 0.6}, "nodes": [
                  {"id": "terminal", "type": "terminal"},
                  {"id": "grid", "grid": {"deliverable": 100, "shares": {"lng": 0.4, "crude": 0.1},
                                            "rationed": "lng", "ibar": {"lng": 50}}},
                  {"id": "fab", "fab": {"grid": "grid"}}]}}}
    obs = {"week": np.asarray([1]), "stock.qty": np.asarray([10., 5., 0., 0.]),
           "stock.qty.observed": np.ones(4), "pipeline.qty.observed": np.zeros(1),
           "graph_now.grid.G_bar": np.asarray([100.]), "graph_now.grid.G_bar.observed": np.ones(1)}
    return config, network, obs


def test_default_is_exact_identity(module):
    config, network, obs = case()
    flows = np.asarray([10., 5.])
    assert module.FuelBatch(config, network).apply(flows, obs) is flows


def test_underfunded_crude_batch_is_held_without_changing_lng(module):
    config, network, obs = case()
    flows = np.asarray([10., 5.])
    policy = module.FuelBatch(config, network, crude=1)
    np.testing.assert_array_equal(policy.apply(flows, obs), [10, 0])
    np.testing.assert_array_equal(flows, [10, 5])
    assert policy.last_held == ((1, 1),)


def test_rationed_fuel_waits_for_next_week_reserve(module):
    config, network, obs = case()
    policy = module.FuelBatch(config, network, lng=1)
    flows = np.asarray([10., 5.])
    assert policy.apply(flows, obs)[0] == 0
    obs["stock.qty"][0] = 30
    flows[0] = 30
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


def test_healthy_rationed_stock_keeps_control(module):
    config, network, obs = case()
    obs["stock.qty"][2] = 30
    flows = np.asarray([10., 5.])
    np.testing.assert_array_equal(module.FuelBatch(config, network, lng=1).apply(flows, obs), flows)


def test_crude_can_wait_for_rationed_gas_pulse(module):
    config, network, obs = case()
    obs["stock.qty"][1] = 10
    flows = np.asarray([10., 10.])
    policy = module.FuelBatch(config, network, lng=1, crude=1, sync_crude=True)
    np.testing.assert_array_equal(policy.apply(flows, obs), [0, 0])
    obs["stock.qty"][2] = 30
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)
    obs["stock.qty.observed"][2] = 0
    obs["stock.qty"][2] = np.nan
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


def test_sync_margin_does_not_hold_crude_when_industry_can_still_receive_energy(module):
    config, network, obs = case()
    obs["stock.qty"][1] = 10
    flows = np.asarray([10., 10.])
    obs["graph_now.grid.y_bar"] = np.asarray([70.])
    obs["graph_now.grid.y_bar.observed"] = np.ones(1)
    policy = module.FuelBatch(config, network, lng=1, crude=1, sync_crude=True, sync_margin_only=True)
    np.testing.assert_array_equal(policy.apply(flows, obs), [0, 0])
    obs["graph_now.grid.y_bar"][:] = 40
    np.testing.assert_array_equal(policy.apply(flows, obs), [0, 10])


def test_complete_pulse_retains_excess_for_next_batch(module):
    config, network, obs = case()
    obs["stock.qty"][[0, 2]] = [20, 35]
    flows = np.asarray([20., 5.])
    result = module.FuelBatch(config, network, lng=1, complete_pulse=True).apply(flows, obs)
    assert result[0] == pytest.approx(5)
    assert result[1] == 5


def test_complete_pulse_does_not_throttle_well_buffered_grid(module):
    config, network, obs = case()
    obs["stock.qty"][[0, 2]] = [40, 60]
    flows = np.asarray([40., 5.])
    result = module.FuelBatch(config, network, lng=1, complete_pulse=True).apply(flows, obs)
    np.testing.assert_array_equal(result, flows)


def test_no_industrial_margin_retains_control_without_holding(module):
    config, network, obs = case()
    obs["graph_now.grid.y_bar"] = np.asarray([100.])
    obs["graph_now.grid.y_bar.observed"] = np.ones(1)
    flows = np.asarray([10., 5.])
    policy = module.FuelBatch(config, network, lng=1, crude=1, power_margin_only=True)
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


def test_hidden_load_padding_uses_public_nominal_not_numeric_payload(module):
    config, network, obs = case()
    config["static"]["instance"]["nodes"][1]["grid"]["base_load"] = 100
    obs["graph_now.grid.y_bar"] = np.asarray([np.nan])
    obs["graph_now.grid.y_bar.observed"] = np.zeros(1)
    flows = np.asarray([10., 5.])
    policy = module.FuelBatch(config, network, lng=1, crude=1, power_margin_only=True)
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


@pytest.mark.parametrize("buffer", [0, 2, 8])
def test_end_aware_cutoff_includes_both_dispatch_boundaries(module, buffer):
    config, network, obs = case()
    config["T"] = 20
    nodes = config["static"]["instance"]["nodes"]
    nodes[2]["fab"].update(product="raw", tau=6)
    nodes.extend([{"id": "osat", "osat": {"packages": {"raw": "packed"}, "tau": 2}},
                  {"id": "sink", "sink": {}}])
    network.node_names += ("osat", "sink")
    network.commodity_names += ("raw", "packed")
    network.routes += (SimpleNamespace(slot_id=2, source_node=2, destination_node=3, commodity_id=2,
                                        chokepoints=(), nominal_transit_weeks=1),
                       SimpleNamespace(slot_id=3, source_node=3, destination_node=4, commodity_id=3,
                                        chokepoints=(), nominal_transit_weeks=1))
    policy = module.FuelBatch(config, network, lng=1, crude=1, end_aware=True, end_buffer=buffer)
    assert policy.latest_week[1] == 8 - buffer
    flows = np.asarray([10., 5., 7., 9.])
    obs["week"][:] = 8 - buffer
    np.testing.assert_array_equal(policy.apply(flows, obs), [0, 0, 7, 9])
    obs["week"][:] = 9 - buffer
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)


@pytest.mark.parametrize("buffer", [-1, 27, 1.5, True, float("nan")])
def test_end_buffer_rejects_invalid_weeks_even_when_disabled(module, buffer):
    config, network, _obs = case()
    with pytest.raises(ValueError, match="end_buffer"):
        module.FuelBatch(config, network, end_buffer=buffer)


@pytest.mark.parametrize("hidden", [0, 2])
def test_hidden_source_or_grid_stock_does_not_trigger_hold(module, hidden):
    config, network, obs = case()
    obs["stock.qty.observed"][hidden] = 0
    obs["stock.qty"][hidden] = np.nan
    flows = np.asarray([10., 5.])
    np.testing.assert_array_equal(module.FuelBatch(config, network, lng=1).apply(flows, obs), flows)


def test_known_current_grid_arrival_counts_once(module):
    config, network, obs = case()
    obs["pipeline.qty.observed"][:] = 1
    for key, value in (("qty", 25.), ("edge", 0), ("k", 0), ("arrival_week", 1)):
        obs["pipeline." + key] = np.asarray([value])
        obs["pipeline." + key + ".observed"] = np.ones(1)
    flows = np.asarray([10., 5.])
    np.testing.assert_array_equal(module.FuelBatch(config, network, lng=1).apply(flows, obs), flows)


def load_case():
    config, network, obs = case()
    config["layout"]["fabs"] = [2]
    config["layout"]["stock_slots"].append((2, 2))
    network.commodity_names += ("wafer",)
    nodes = config["static"]["instance"]["nodes"]
    nodes[1]["grid"]["base_load"] = 20
    nodes[2]["fab"].update(input="wafer", cap0=20, e=2)
    obs["stock.qty"] = np.append(obs["stock.qty"], 10.)
    obs["stock.qty.observed"] = np.ones(5)
    for key, value in (("grid.y_bar", 20.), ("fab.R", 1.), ("fab.cap_eff", 20.)):
        obs["graph_now." + key] = np.asarray([value])
        obs["graph_now." + key + ".observed"] = np.ones(1)
    return config, network, obs


def test_load_aware_batch_uses_visible_base_and_wafer_limited_draw(module):
    config, network, obs = load_case()
    policy = module.FuelBatch(config, network, crude=1, load_aware=True)
    flows = np.asarray([10., 5.])
    np.testing.assert_array_equal(policy.apply(flows, obs), flows)
    # 20 base + 10 wafers * 2 energy = 40% load; crude burn target is 4, not 10.
    assert policy._load_fraction(1, 0, 100., {}, obs) == pytest.approx(0.4)
    obs["stock.qty"][4] = 0
    assert policy._load_fraction(1, 0, 100., {(2, 2): 5}, obs) == pytest.approx(0.3)


def test_load_aware_hidden_inputs_and_marks_use_nominal_not_padding(module):
    config, network, obs = load_case()
    obs["stock.qty.observed"][4] = 0
    obs["stock.qty"][4] = np.nan
    for key in ("grid.y_bar", "fab.R", "fab.cap_eff"):
        obs["graph_now." + key][:] = np.nan
        obs["graph_now." + key + ".observed"][:] = 0
    policy = module.FuelBatch(config, network, crude=1, load_aware=True)
    assert policy._load_fraction(1, 0, 100., {}, obs) == pytest.approx(0.6)


def test_load_aware_down_fab_has_no_energy_draw(module):
    config, network, obs = load_case()
    obs["graph_now.fab.R"][:] = 0
    policy = module.FuelBatch(config, network, crude=1, load_aware=True)
    assert policy._load_fraction(1, 0, 100., {}, obs) == pytest.approx(0.2)


@pytest.mark.parametrize("factor", [True, -1, float("nan"), "1"])
def test_bad_batch_factor_rejected(module, factor):
    config, network, _obs = case()
    with pytest.raises(ValueError, match="factor"):
        module.FuelBatch(config, network, lng=factor)
