"""Shared OSAT stock and receiver demand are accounted for exactly once."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "sales_dispatch", raising=False)
    return importlib.import_module("sales_dispatch")


def case():
    routes = tuple(SimpleNamespace(slot_id=i, source_node=source, destination_node=sink,
                                   commodity_id=0, edge_id=i, edges=(i,), nominal_freight_per_unit=1)
                   for i, (source, sink) in enumerate(((0, 2), (1, 2), (0, 3), (1, 3))))
    network = SimpleNamespace(node_names=("osat_a", "osat_b", "sink_a", "sink_b"),
                              commodity_names=("chip",), routes=routes, lane_edges=(),
                              edge_transit_weeks=(1, 1, 1, 1), edge_head=(2, 2, 3, 3), chokepoints=())
    config = {"T": 10, "layout": {"stock_slots": [(0, 0), (1, 0), (2, 0), (3, 0)],
                                  "demands": [(2, 0), (3, 0)]},
              "static": {"commodities": {"v": [1]}, "sinks": {"pi": [100, 100]},
                         "instance": {"nodes": [{"id": "osat_a"}, {"id": "osat_b"},
                                     {"id": "sink_a", "sink": {"demand": {"chip": {"dbar": 20}}}},
                                     {"id": "sink_b", "sink": {"demand": {"chip": {"dbar": 80}}}}]}}}
    obs = {"week": np.asarray([1]), "stock.qty": np.asarray([100., 100., 0., 0.]),
           "stock.qty.observed": np.ones(4), "pipeline.qty.observed": np.zeros(1),
           "graph_now.u": np.full(4, 100.), "graph_now.u.observed": np.ones(4),
           "graph_now.tau": np.ones(4), "graph_now.tau.observed": np.ones(4),
           "graph_now.c": np.ones(4), "graph_now.c.observed": np.ones(4),
           "graph_now.tariff": np.zeros((4, 1)), "graph_now.tariff.observed": np.ones((4, 1)),
           "backlog.qty": np.zeros(2), "backlog.qty.observed": np.ones(2),
           "demand_forecast.qty": np.asarray([[20., 20.], [80., 80.]]),
           "demand_forecast.qty.observed": np.ones((2, 2))}
    return config, network, obs


def test_default_is_control_identity(module):
    config, network, obs = case()
    flows = np.full(4, 100.)
    assert module.SalesDispatch(config, network).apply(flows, flows, obs) is flows


def test_multiple_sources_cannot_duplicate_receiver_need(module):
    config, network, obs = case()
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1).apply(flows, flows, obs)
    assert out[:2].sum() == pytest.approx(40)
    assert out[2:].sum() == pytest.approx(160)
    assert out[[0, 2]].sum() <= 100 + 1e-8
    assert out[[1, 3]].sum() <= 100 + 1e-8


def test_request_mask_and_shared_edge_are_preserved(module):
    config, network, obs = case()
    network.routes[1].edge_id = 0
    requests = np.asarray([100., 100., 0., 100.])
    obs["graph_now.u"][0] = 30
    out = module.SalesDispatch(config, network, cover=2).apply(requests, requests, obs)
    assert out[2] == 0
    assert out[:2].sum() <= 30 + 1e-8


def test_current_stock_covers_demand_without_new_dispatch(module):
    config, network, obs = case()
    obs["stock.qty"][2:] = 1000
    flows = np.full(4, 100.)
    np.testing.assert_array_equal(module.SalesDispatch(config, network, cover=1).apply(flows, flows, obs), np.zeros(4))


def test_known_arrivals_cover_the_same_receiver_once(module):
    config, network, obs = case()
    obs["pipeline.qty.observed"][:] = 1
    for key, value in (("qty", 40.), ("edge", 0), ("k", 0), ("arrival_week", 2), ("lane", 0)):
        obs[f"pipeline.{key}"] = np.asarray([value])
        obs[f"pipeline.{key}.observed"] = np.asarray([0 if key == "lane" else 1])
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1).apply(flows, flows, obs)
    assert out[:2].sum() == 0


def test_unknown_arrival_time_does_not_cancel_need(module):
    config, network, obs = case()
    obs["pipeline.qty.observed"][:] = 1
    for key, value in (("qty", 1000.), ("edge", 0), ("k", 0), ("arrival_week", -999)):
        obs[f"pipeline.{key}"] = np.asarray([value])
        obs[f"pipeline.{key}.observed"] = np.asarray([0 if key == "arrival_week" else 1])
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1).apply(flows, flows, obs)
    assert out[:2].sum() == pytest.approx(40)


def test_no_dispatch_can_serve_beyond_episode_horizon(module):
    config, network, obs = case()
    obs["week"][0] = 10
    flows = np.full(4, 100.)
    np.testing.assert_array_equal(module.SalesDispatch(config, network, cover=1).apply(flows, flows, obs), np.zeros(4))


def test_hidden_stock_and_forecast_padding_are_not_used(module):
    config, network, obs = case()
    obs["stock.qty.observed"][2] = 0
    obs["demand_forecast.qty.observed"][1] = 0
    flows = np.full(4, 100.)
    policy = module.SalesDispatch(config, network, cover=1)
    expected = policy.apply(flows, flows, obs)
    obs["stock.qty"][2] = np.nan
    obs["demand_forecast.qty"][1] = np.nan
    np.testing.assert_array_equal(expected, policy.apply(flows, flows, obs))


def fab_case():
    config, network, obs = case()
    network.commodity_names = ("wafer", "chip_raw", "chip")
    config["layout"]["demands"] = [(3, 2)]
    config["layout"]["fabs"] = [2]
    nodes = config["static"]["instance"]["nodes"]
    nodes[2] = {"id": "sink_a", "fab": {"input": "wafer", "product": "chip_raw", "cap0": 10, "tau": 3}}
    nodes[3]["sink"]["demand"] = {"chip": {"dbar": 80}}
    config["static"]["sinks"]["pi"] = [100]
    config["static"]["commodities"]["v"] = [1, 2, 3]
    obs["graph_now.fab.cap_eff"] = np.asarray([5.])
    obs["graph_now.fab.cap_eff.observed"] = np.ones(1)
    return config, network, obs


def test_fab_coverage_uses_real_fab_index_not_sink_forecast(module):
    config, network, obs = fab_case()
    obs["demand_forecast.qty"][:] = np.nan
    obs["backlog.qty"][:] = np.nan
    flows = np.full(4, 100.)
    policy = module.SalesDispatch(config, network, cover=1, stage="fab")
    out = policy.apply(flows, flows, obs)
    assert out[:2].sum() == pytest.approx(10)
    np.testing.assert_array_equal(out[2:], flows[2:])


def test_unknown_fab_capacity_uses_nominal_not_numeric_padding(module):
    config, network, obs = fab_case()
    obs["graph_now.fab.cap_eff.observed"][:] = 0
    obs["graph_now.fab.cap_eff"][:] = np.nan
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1, stage="fab").apply(flows, flows, obs)
    assert out[:2].sum() == pytest.approx(20)


def test_input_wip_is_not_counted_as_unconsumed_wafer(module):
    config, network, obs = fab_case()
    obs["wip.qty"] = np.asarray([1000.])
    obs["wip.qty.observed"] = np.ones(1)
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1, stage="fab").apply(flows, flows, obs)
    assert out[:2].sum() == pytest.approx(10)


def test_production_lead_time_limits_last_input_dispatch(module):
    config, network, obs = fab_case()
    obs["week"][:] = 8
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1, stage="fab").apply(flows, flows, obs)
    assert not out[:2].any()


def test_raw_coverage_uses_osat_throughput_and_stock_mix(module):
    config, network, obs = fab_case()
    config["layout"]["osats"] = [2]
    nodes = config["static"]["instance"]["nodes"]
    nodes[2] = {"id": "sink_a", "osat": {"packages": {"wafer": "chip"}, "thr": 10, "tau": 2},
                "stock": {"wafer": {"storage": 100}}}
    obs["graph_now.osat.thr_eff"] = np.asarray([7.])
    obs["graph_now.osat.thr_eff.observed"] = np.ones(1)
    flows = np.full(4, 100.)
    out = module.SalesDispatch(config, network, cover=1, stage="raw").apply(flows, flows, obs)
    assert out[:2].sum() == pytest.approx(14)
