"""Scarce-stock redistribution preserves budgets, masks and visibility."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "rebalance", raising=False)
    return importlib.import_module("rebalance")


def case():
    routes = tuple(
        SimpleNamespace(slot_id=i, source_node=0, destination_node=i + 1, commodity_id=0 if i < 2 else 1, edge_id=i)
        for i in range(3)
    )
    network = SimpleNamespace(routes=routes, node_names=("source", "left", "right", "sink"),
                              commodity_names=("lng", "chip"), edges_to_slots=((0,), (1,), (2,)),
                              slots_from={(0, 0): (0, 1), (0, 1): (2,)})
    nodes = [
        {"id": "source"},
        {"id": "left", "grid": {"deliverable": 50, "shares": {"lng": 1}}},
        {"id": "right", "grid": {"deliverable": 50, "shares": {"lng": 1}}},
        {"id": "sink", "sink": {"demand": {"chip": {"dbar": 10}}}},
    ]
    config = {"static": {"instance": {"nodes": nodes}},
              "layout": {"stock_slots": [(0, 0), (0, 1), (1, 0), (2, 0), (3, 1)],
                         "grids": [1, 2], "demands": [(3, 1)]}}
    obs = {"stock.qty": np.asarray([100., 100., 0., 100., 0.]),
           "stock.qty.observed": np.ones(5, dtype=np.int8),
           "graph_now.u": np.full(3, 100.), "graph_now.u.observed": np.ones(3, dtype=np.int8),
           "demand_forecast.qty": np.asarray([[10., 10.]]),
           "demand_forecast.qty.observed": np.ones((1, 2), dtype=np.int8)}
    return config, network, obs


def test_disabled_is_exact_control_identity(module):
    config, network, obs = case()
    flows = np.full(3, 100.)
    assert module.StockRebalancer(config, network).apply(flows, obs) is flows


def output_case():
    routes = tuple(SimpleNamespace(slot_id=i, source_node=0, destination_node=i + 1,
                                   commodity_id=0, edge_id=i) for i in range(2))
    network = SimpleNamespace(routes=routes, node_names=("source", "left", "right"),
                              commodity_names=("wafer", "raw"), edges_to_slots=((0,), (1,)),
                              slots_from={(0, 0): (0, 1)})
    fab = {"input": "wafer", "product": "raw", "cap0": 10, "grid": None}
    nodes = [{"id": "source"}, *({"id": name, "fab": dict(fab), "stock": {"raw": {"storage": 100}}}
                                  for name in ("left", "right"))]
    config = {"static": {"instance": {"nodes": nodes}},
              "layout": {"stock_slots": [(0, 0), (1, 0), (2, 0), (1, 1), (2, 1)],
                         "fabs": [1, 2], "grids": [], "demands": []}}
    obs = {"stock.qty": np.asarray([10., 0., 0., 100., 0.]), "stock.qty.observed": np.ones(5),
           "graph_now.u": np.full(2, 10.), "graph_now.u.observed": np.ones(2)}
    return config, network, obs


@pytest.mark.parametrize("hidden", [False, True])
def test_output_congestion_redistributes_inputs_without_reading_hidden_padding(module, hidden):
    config, network, obs = output_case()
    policy = module.StockRebalancer(config, network, production_output_power=1)
    if hidden:
        obs["stock.qty.observed"][3] = 0
        obs["stock.qty"][3] = np.nan
    out = policy.apply(np.full(2, 10.), obs)
    assert out.sum() == pytest.approx(10)
    assert np.all(out >= 0) and np.all(out <= 10)
    if hidden:
        np.testing.assert_allclose(out, [5, 5])
    else:
        assert out[0] < out[1]
        np.testing.assert_allclose(out, [10 / 21, 200 / 21])


@pytest.mark.parametrize("hidden", [None, "qty", "node", "k", "out_week"])
def test_output_wip_proxy_requires_visible_metadata_and_near_completion(module, hidden):
    config, network, obs = output_case()
    obs["stock.qty"][3] = 50
    obs["week"] = np.asarray([5])
    for field, values in (("qty", [50., 1000., 1000.]), ("node", [1., 2., 2.]),
                          ("k", [1., 1., 1.]), ("out_week", [6., 8., 4.])):
        obs[f"wip.{field}"] = np.asarray(values)
        obs[f"wip.{field}.observed"] = np.ones(3)
    if hidden:
        obs[f"wip.{hidden}.observed"][0] = 0
        obs[f"wip.{hidden}"][0] = np.nan
    policy = module.StockRebalancer(config, network, production_output_power=1, production_wip_horizon=2)
    out = policy.apply(np.full(2, 10.), obs)
    np.testing.assert_allclose(out, [10 / 3, 20 / 3] if hidden else [10 / 21, 200 / 21])
    np.testing.assert_allclose(module.StockRebalancer(config, network, production_output_power=1).apply(
        np.full(2, 10.), obs), [10 / 3, 20 / 3])


@pytest.mark.parametrize("value", [True, -1, 1.5, "1"])
def test_invalid_production_wip_horizon_is_rejected(module, value):
    config, network, _obs = output_case()
    with pytest.raises(ValueError, match="production_wip_horizon"):
        module.StockRebalancer(config, network, production_wip_horizon=value)


@pytest.mark.parametrize("value", [True, -1, float("nan"), "1"])
def test_invalid_production_output_power_is_rejected(module, value):
    config, network, _obs = case()
    with pytest.raises(ValueError, match="production_output_power"):
        module.StockRebalancer(config, network, production_output_power=value)


def test_empty_receiver_gets_more_without_losing_dispatch_budget(module):
    config, network, obs = case()
    flows = np.full(3, 100.)
    out = module.StockRebalancer(config, network, fuel_power=1).apply(flows, obs)
    np.testing.assert_allclose(out, [200 / 3, 100 / 3, 100])
    assert out[:2].sum() == pytest.approx(100)
    np.testing.assert_array_equal(flows, [100, 100, 100])


def test_no_source_scarcity_keeps_each_receiver_at_request(module):
    config, network, obs = case()
    obs["stock.qty"][0] = 300
    flows = np.full(3, 100.)
    np.testing.assert_array_equal(flows, module.StockRebalancer(config, network, fuel_power=4).apply(flows, obs))


def test_visible_reduced_generation_changes_conditional_fuel_coverage(module):
    config, network, obs = case()
    obs["graph_now.grid.G_bar"] = np.asarray([0., 50.])
    obs["graph_now.grid.G_bar.observed"] = np.ones(2)
    obs["stock.qty"][[2, 3]] = 50
    policy = module.StockRebalancer(config, network, fuel_power=1, fuel_mark_rate_floor=0.25)
    assert policy.fuel_rate((1, 0), obs) == 12.5
    assert policy.coverage((1, 0), obs)[0] == 4
    out = policy.apply(np.full(3, 100.), obs)
    assert out[0] < out[1]
    assert out[:2].sum() == pytest.approx(100)


def test_unobserved_generation_uses_nominal_not_padding(module):
    config, network, obs = case()
    obs["graph_now.grid.G_bar"] = np.full(2, np.nan)
    obs["graph_now.grid.G_bar.observed"] = np.zeros(2)
    policy = module.StockRebalancer(config, network, fuel_power=1, fuel_mark_rate_floor=0.25)
    assert policy.fuel_rate((1, 0), obs) == 50
    assert policy.fuel_rate((2, 0), obs) == 50
    assert np.all(np.isfinite(policy.apply(np.full(3, 100.), obs)))


@pytest.mark.parametrize("value", [True, -0.5, 1.1, float("nan"), "0.25"])
def test_invalid_fuel_mark_rate_floor_is_rejected(module, value):
    config, network, _ = case()
    with pytest.raises(ValueError, match="fuel_mark_rate_floor"):
        module.StockRebalancer(config, network, fuel_mark_rate_floor=value)


def test_prohibited_zero_route_is_not_reactivated(module):
    config, network, obs = case()
    flows = np.asarray([0., 150., 100.])
    out = module.StockRebalancer(config, network, fuel_power=4).apply(flows, obs)
    assert out[0] == 0 and out[1] == 100


@pytest.mark.parametrize("source_hidden", [False, True])
def test_hidden_stock_padding_cannot_change_distribution(module, source_hidden):
    config, network, obs = case()
    row = 0 if source_hidden else 3
    obs["stock.qty.observed"][row] = 0
    policy = module.StockRebalancer(config, network, fuel_power=2)
    flows = np.full(3, 100.)
    expected = policy.apply(flows, obs)
    obs["stock.qty"][row] = np.nan
    np.testing.assert_array_equal(expected, policy.apply(flows, obs))
    if not source_hidden:
        np.testing.assert_allclose(expected[:2], [50, 50])


def test_joint_entry_edge_capacity_is_shared_across_commodities(module):
    config, network, obs = case()
    network.routes[2].edge_id = 0
    network.edges_to_slots = ((0, 2), (1,), ())
    out = module.StockRebalancer(config, network, fuel_power=1).apply(np.full(3, 100.), obs)
    assert out[0] + out[2] <= 100 + 1e-10
    assert out[0] + out[1] == pytest.approx(100)


def test_stock_first_frees_capacity_reserved_for_absent_commodity(module):
    config, network, obs = case()
    network.routes[2].edge_id = 0
    network.edges_to_slots = ((0, 2), (1,), ())
    obs["stock.qty"][0] = 200
    obs["stock.qty"][1] = 0
    out = module.StockRebalancer(config, network, stock_first=True).apply(np.full(3, 100.), obs)
    np.testing.assert_array_equal(out, [100, 100, 0])


def test_stock_first_never_reads_hidden_stock_padding(module):
    config, network, obs = case()
    obs["stock.qty.observed"][0] = 0
    policy = module.StockRebalancer(config, network, stock_first=True)
    expected = policy.apply(np.full(3, 100.), obs)
    obs["stock.qty"][0] = np.nan
    np.testing.assert_array_equal(expected, policy.apply(np.full(3, 100.), obs))


def test_stock_first_requires_boolean(module):
    config, network, _ = case()
    with pytest.raises(ValueError, match="stock_first"):
        module.StockRebalancer(config, network, stock_first=1)


def test_spare_preserves_feasible_flows_and_recovers_empty_commodity_capacity(module):
    config, network, obs = case()
    network.routes[2].edge_id = 0
    network.edges_to_slots = ((0, 2), (1,), ())
    obs["stock.qty"][0] = 200
    obs["stock.qty"][1] = 0
    out = module.StockRebalancer(config, network, fill_spare=True).apply(np.full(3, 100.), obs)
    np.testing.assert_allclose(out, [100, 100, 0])


def test_spare_never_exceeds_source_or_joint_edge_budget(module):
    config, network, obs = case()
    network.routes[2].edge_id = 0
    network.edges_to_slots = ((0, 2), (1,), ())
    obs["stock.qty"][0] = 50
    obs["stock.qty"][1] = 20
    out = module.StockRebalancer(config, network, fill_spare=True).apply(np.full(3, 100.), obs)
    assert out[:2].sum() <= 50 + 1e-8
    assert out[2] <= 20 + 1e-8
    assert out[0] + out[2] <= 100 + 1e-8


def test_spare_leaves_masked_slots_exactly_zero(module):
    config, network, obs = case()
    out = module.StockRebalancer(config, network, fill_spare=True).apply(np.asarray([0., 100., 100.]), obs)
    assert out[0] == 0


def test_rate_weighting_tracks_receiver_burn_without_losing_dispatch(module):
    config, network, obs = case()
    obs["stock.qty"][3] = 0
    config["static"]["instance"]["nodes"][2]["grid"]["deliverable"] = 100
    out = module.StockRebalancer(config, network, fuel_power=1, rate_power=1).apply(np.full(3, 100.), obs)
    np.testing.assert_allclose(out[:2], [100 / 3, 200 / 3])


def pipeline_case():
    config, network, obs = case()
    network.lane_edges, network.chokepoints = (), (0,)
    network.edge_head = (1, 2, 3)
    obs.update({"week": np.asarray([2]), "pipeline.qty": np.asarray([50.]),
                "pipeline.qty.observed": np.ones(1), "pipeline.edge": np.asarray([0]),
                "pipeline.k": np.asarray([0]), "pipeline.arrival_week": np.asarray([3]),
                "pipeline.lane": np.asarray([0]), "pipeline.lane.observed": np.zeros(1)})
    for name in ("edge", "k", "arrival_week"):
        obs[f"pipeline.{name}.observed"] = np.ones(1)
    return config, network, obs


def test_confirmed_off_lane_arrival_from_chokepoint_is_counted(module):
    config, network, obs = pipeline_case()
    coverage, _ = module.StockRebalancer(config, network, fuel_power=1, pipeline_horizon=8).coverage((1, 0), obs)
    assert coverage == 1


@pytest.mark.parametrize("hidden", ["edge", "k", "arrival_week"])
def test_incomplete_pipeline_metadata_is_not_assumed_to_arrive(module, hidden):
    config, network, obs = pipeline_case()
    obs[f"pipeline.{hidden}.observed"][:] = 0
    obs[f"pipeline.{hidden}"][:] = -999
    coverage, _ = module.StockRebalancer(config, network, fuel_power=1, pipeline_horizon=8).coverage((1, 0), obs)
    assert coverage == 0


def test_pipeline_arriving_at_chokepoint_has_unknown_final_arrival(module):
    config, network, obs = pipeline_case()
    network.edge_head = (0, 2, 3)
    coverage, _ = module.StockRebalancer(config, network, fuel_power=1, pipeline_horizon=8).coverage((1, 0), obs)
    assert coverage == 0


def test_terminal_coverage_includes_connected_grid_once(module):
    config, network, obs = case()
    network.node_names = (*network.node_names, "terminal")
    config["static"]["instance"]["nodes"].append({"id": "terminal"})
    config["layout"]["stock_slots"].append((4, 0))
    network.routes[0].destination_node = 4
    network.routes = (*network.routes, SimpleNamespace(slot_id=3, source_node=4, destination_node=1,
                                                       commodity_id=0, edge_id=3))
    network.edges_to_slots = (*network.edges_to_slots, (3,))
    network.slots_from[4, 0] = (3,)
    obs["stock.qty"] = np.asarray([100., 100., 150., 100., 0., 50.])
    obs["stock.qty.observed"] = np.ones(6, dtype=np.int8)
    obs["graph_now.u"] = np.full(4, 100.)
    obs["graph_now.u.observed"] = np.ones(4, dtype=np.int8)
    policy = module.StockRebalancer(config, network, fuel_power=1)
    coverage, _ = policy._coverage(network.routes[0], obs)
    assert coverage == 4
    out = policy.apply(np.full(4, 100.), obs)
    assert out[0] < out[1]


def test_waterfill_saturates_routes_and_conserves_total(module):
    np.testing.assert_allclose(module.waterfill([10, 100], [100, 1], 50), [10, 40])
    np.testing.assert_array_equal(module.waterfill([10, 100], [1, 1], 200), [10, 100])
    np.testing.assert_array_equal(module.waterfill([10, 100], [1, 1], 0), [0, 0])


def test_waterfill_random_budget_invariants(module):
    rng = np.random.default_rng(0)
    for _ in range(100):
        caps = rng.uniform(0, 100, size=20)
        priorities = rng.uniform(0, 1, size=20)
        budget = rng.uniform(0, caps.sum() * 1.2)
        out = module.waterfill(caps, priorities, budget)
        assert np.all(out >= 0) and np.all(out <= caps + 1e-10)
        assert out.sum() == pytest.approx(min(budget, caps.sum()))


def production_case():
    network = SimpleNamespace(
        node_names=("source", "fab_left", "fab_right", "grid_left", "grid_right", "sink_left", "sink_right"),
        commodity_names=("wafer", "chip_le_raw", "chip_mat_raw", "chip_le", "chip_mat", "lng"),
        routes=tuple(SimpleNamespace(slot_id=i, source_node=0, destination_node=i + 1, commodity_id=0,
                                     edge_id=i) for i in range(2)),
        edges_to_slots=((0,), (1,)), slots_from={(0, 0): (0, 1)},
    )
    nodes = [{"id": "source"}]
    for side, product in (("left", "chip_le_raw"), ("right", "chip_mat_raw")):
        nodes.append({"id": "fab_" + side, "fab": {"input": "wafer", "product": product,
                                                  "grid": "grid_" + side, "cap0": 10, "e": 2}})
    for side in ("left", "right"):
        nodes.append({"id": "grid_" + side, "grid": {"deliverable": 100, "priority": "base_first",
                     "shares": {"lng": 0.4, "unmodelled": 0.6}, "ibar": {"lng": 50},
                     "rationed": "lng", "voll": 1}})
    for side, product, penalty in (("left", "chip_le", 100), ("right", "chip_mat", 50)):
        nodes.append({"id": "sink_" + side, "sink": {"demand": {product: {"dbar": 10, "pi": penalty}}}})
    config = {"layout": {"fabs": [1, 2], "grids": [3, 4], "demands": [(5, 3), (6, 4)],
                         "stock_slots": [(0, 0), (1, 0), (2, 0), (3, 5), (4, 5)]},
              "static": {"instance": {"nodes": nodes, "params": {"psi": 0.6}}}}
    obs = {"stock.qty": np.asarray([100., 0., 0., 40., 0.]), "stock.qty.observed": np.ones(5),
           "graph_now.u": np.full(2, 100.), "graph_now.u.observed": np.ones(2)}
    for key, value in (("fab.cap_eff", 10.), ("fab.R", 1.), ("grid.G_bar", 100.), ("grid.y_bar", 60.)):
        obs["graph_now." + key] = np.full(2, value)
        obs["graph_now." + key + ".observed"] = np.ones(2)
    return config, network, obs


def test_fuel_margin_is_bounded_and_zero_without_industrial_headroom(module):
    config, network, obs = production_case()
    obs["graph_now.fab.alpha_bar"] = np.ones(2)
    obs["graph_now.fab.alpha_bar.observed"] = np.ones(2)
    policy = module.StockRebalancer(config, network, fuel_power=1, fuel_margin_bonus=1)
    assert 1 < policy.fuel_priority((3, 5), obs) <= 4
    obs["graph_now.grid.y_bar"][0] = 100
    assert policy.fuel_priority((3, 5), obs) == 1


def test_fuel_margin_hidden_generation_does_not_read_padding(module):
    config, network, obs = production_case()
    obs["graph_now.fab.alpha_bar"] = np.full(2, np.nan)
    obs["graph_now.fab.alpha_bar.observed"] = np.zeros(2)
    policy = module.StockRebalancer(config, network, fuel_power=1, fuel_margin_bonus=1)
    obs["graph_now.grid.G_bar.observed"][0] = 0
    obs["graph_now.grid.G_bar"][0] = np.nan
    assert policy.fuel_priority((3, 5), obs) == 1


def test_production_value_priority_conserves_stock_and_favors_higher_penalty(module):
    config, network, obs = production_case()
    out = module.StockRebalancer(config, network, production_power=2, production_value_power=1).apply(
        np.full(2, 100.), obs)
    np.testing.assert_allclose(out, [200 / 3, 100 / 3])


def test_production_energy_proxy_retains_floor_for_currently_unpowered_fab(module):
    config, network, obs = production_case()
    policy = module.StockRebalancer(config, network, production_power=2, production_energy_power=1)
    np.testing.assert_allclose(policy.apply(np.full(2, 100.), obs), [80, 20])
    obs["stock.qty"][3] = 15
    assert policy.production_priority((1, 0), obs) == pytest.approx(0.8125)


def test_production_energy_unknown_stock_and_grid_do_not_read_padding(module):
    config, network, obs = production_case()
    policy = module.StockRebalancer(config, network, production_power=2, production_energy_power=1)
    obs["stock.qty.observed"][4] = 0
    expected = policy.apply(np.full(2, 100.), obs)
    obs["stock.qty"][4] = np.nan
    np.testing.assert_array_equal(policy.apply(np.full(2, 100.), obs), expected)
    obs["graph_now.grid.G_bar.observed"][:] = 0
    obs["graph_now.grid.G_bar"][:] = np.nan
    np.testing.assert_allclose(policy.apply(np.full(2, 100.), obs), [50, 50])


def test_production_industrial_first_does_not_subtract_base_load(module):
    config, network, obs = production_case()
    config["static"]["instance"]["nodes"][4]["grid"]["priority"] = "industrial_first"
    policy = module.StockRebalancer(config, network, production_power=2, production_energy_power=1)
    assert policy.production_priority((2, 0), obs) == 1


@pytest.mark.parametrize("name", ["fuel_power", "sink_power", "cover_floor"])
@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "1"])
def test_invalid_parameters_are_rejected(module, name, value):
    config, network, _ = case()
    with pytest.raises(ValueError, match=name):
        module.StockRebalancer(config, network, **{name: value})
