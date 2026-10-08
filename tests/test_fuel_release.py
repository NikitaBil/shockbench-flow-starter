"""Tanker releases respect visible quantities, masks and shared budgets."""

import importlib
import sys

import numpy as np
import pytest

from tests.conftest import ROOT
from tests.test_rebalance import case


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents/team_agent"))
    monkeypatch.delitem(sys.modules, "fuel_release", raising=False)
    return importlib.import_module("fuel_release")


def release_case():
    config, network, obs = case()
    config["static"]["nodes"] = {"type": ["chokepoint", "grid", "grid", "sink"]}
    network.edge_head = (1, 2, 3)
    network.edge_tail = (0, 0, 0)
    network.edge_transit_weeks = (1, 1, 1)
    network.lane_edges = ()
    config["layout"].update(chokepoints=[0], release_pairs=[(0, 0)], lot_keys=[(0, 0, None, 0)])
    config["static"]["override_slots"] = {
        "chokepoint": [0, 0], "k": [0, 0], "out_edge": [0, 1], "lane": [None, None],
    }
    obs.update({
        "week": np.asarray([2]), "queue_lots.qty": np.asarray([[100., 0.]]),
        "queue_lots.qty.observed": np.asarray([[1, 0]]),
        "override_mask": np.asarray([1, 1]), "override_mask.observed": np.asarray([1]),
        "pipeline.qty": np.asarray([]), "pipeline.qty.observed": np.asarray([]),
        "graph_now.kappa.tb": np.asarray([100.]), "graph_now.kappa.tb.observed": np.asarray([1]),
        "graph_now.prohibited": np.zeros((3, 2)), "graph_now.prohibited.observed": np.ones((3, 2)),
        "graph_now.tau": np.ones(3), "graph_now.tau.observed": np.ones(3),
        "graph_now.open": np.ones(1), "graph_now.open.observed": np.ones(1),
    })
    return config, network, obs


def test_disabled_keeps_default_release(module):
    config, network, obs = release_case()
    action = {"flows": np.zeros(3)}
    assert module.FuelRelease(config, network).apply(action, obs) is action


def test_redirects_to_lower_stock_and_conserves_visible_queue(module):
    config, network, obs = release_case()
    action = module.FuelRelease(config, network, power=2).apply({"flows": np.zeros(3)}, obs)
    assert action["override_qty"][0] > action["override_qty"][1]
    assert action["override_qty"].sum() == pytest.approx(100)
    np.testing.assert_array_equal(action["release_mode"], [1])


def test_prohibited_receiver_gets_exactly_zero(module):
    config, network, obs = release_case()
    obs["override_mask"][0] = 0
    action = module.FuelRelease(config, network, power=2).apply({"flows": np.zeros(3)}, obs)
    np.testing.assert_array_equal(action["override_qty"], [0, 100])


def test_blackout_retains_default_fifo(module):
    config, network, obs = release_case()
    obs["override_mask.observed"][0] = 0
    action = {"flows": np.zeros(3)}
    assert module.FuelRelease(config, network, power=2).apply(action, obs) is action


def test_hidden_queue_padding_is_not_cargo(module):
    config, network, obs = release_case()
    obs["queue_lots.qty"][0, 1] = np.nan
    action = module.FuelRelease(config, network, power=2).apply({"flows": np.zeros(3)}, obs)
    assert np.isfinite(action["override_qty"]).all()
    assert action["override_qty"].sum() == pytest.approx(100)


def test_sparse_queue_layout_matches_dense_release_and_ignores_padding(module):
    config, network, obs = release_case()
    expected = module.FuelRelease(config, network, power=1, preserve_routes=True).apply({"flows": np.zeros(3)}, obs)
    config["layout"].pop("lot_keys")
    obs["queue_lots.qty"] = np.asarray([100., np.nan])
    obs["queue_lots.qty.observed"] = np.asarray([1, 0])
    for field, value in (("chokepoint", 0), ("k", 0), ("lane", -1), ("next_edge", 0)):
        obs[f"queue_lots.{field}"] = np.asarray([value, np.nan])
        obs[f"queue_lots.{field}.observed"] = np.asarray([1, 0])
    actual = module.FuelRelease(config, network, power=1, preserve_routes=True).apply({"flows": np.zeros(3)}, obs)
    np.testing.assert_array_equal(actual["override_qty"], expected["override_qty"])
    obs["queue_lots.k.observed"][:] = 0
    obs["queue_lots.k"][:] = np.nan
    actual = module.FuelRelease(config, network, power=1).apply({"flows": np.zeros(3)}, obs)
    assert not actual["override_qty"].any()


def test_current_chokepoint_arrivals_are_included(module):
    config, network, obs = release_case()
    network.edge_head = (0, 2, 3)
    obs["pipeline.qty"] = np.asarray([25.])
    obs["pipeline.qty.observed"] = np.ones(1)
    obs["pipeline.lane"] = np.zeros(1, dtype=int)
    obs["pipeline.lane.observed"] = np.zeros(1, dtype=int)
    for key, value in (("edge", 0), ("k", 0), ("arrival_week", 2)):
        obs[f"pipeline.{key}"] = np.asarray([value])
        obs[f"pipeline.{key}.observed"] = np.ones(1)
    obs["graph_now.kappa.tb"][0] = 200
    obs["graph_now.u"][:] = 200
    action = module.FuelRelease(config, network, power=2).apply({"flows": np.zeros(3)}, obs)
    assert action["override_qty"].sum() == pytest.approx(125)


def test_throughput_is_a_joint_limit(module):
    config, network, obs = release_case()
    obs["graph_now.kappa.tb"][0] = 30
    action = module.FuelRelease(config, network, power=2).apply({"flows": np.zeros(3)}, obs)
    assert action["override_qty"].sum() == pytest.approx(30)


def test_only_blocked_keeps_default_mode_when_route_is_open(module):
    config, network, obs = release_case()
    action = module.FuelRelease(config, network, power=2, only_blocked=True).apply({"flows": np.zeros(3)}, obs)
    np.testing.assert_array_equal(action["release_mode"], [0])
    assert action["override_qty"].sum() == 0


def test_only_blocked_redirects_prohibited_original_route(module):
    config, network, obs = release_case()
    obs["override_mask"][0] = 0
    action = module.FuelRelease(config, network, power=2, only_blocked=True).apply({"flows": np.zeros(3)}, obs)
    np.testing.assert_array_equal(action["release_mode"], [1])
    np.testing.assert_array_equal(action["override_qty"], [0, 100])


def test_preserved_route_is_not_diverted_merely_for_another_receivers_stock(module):
    config, network, obs = release_case()
    obs["stock.qty"][2] = 1000
    obs["stock.qty"][3] = 0
    action = module.FuelRelease(config, network, power=2, preserve_routes=True).apply({"flows": np.zeros(3)}, obs)
    np.testing.assert_array_equal(action["override_qty"], [100, 0])


def test_resource_safe_extra_cannot_displace_prior_on_shared_edge(module):
    config, network, obs = release_case()
    config["static"]["override_slots"]["out_edge"] = [0, 0]
    config["static"]["override_slots"]["lane"] = [0, 1]
    network.lane_edges = ((0,), (0,))
    config["layout"]["lot_keys"][0] = (0, 0, 0, 0)
    config["layout"]["lot_keys"].append((0, 0, 9, 2))
    obs["queue_lots.qty"] = np.asarray([[100., 0.], [50., 0.]])
    obs["queue_lots.qty.observed"] = np.asarray([[1, 0], [1, 0]])
    obs["graph_now.kappa.tb"][0] = 200
    action = module.FuelRelease(config, network, power=2, preserve_routes=True,
                                resource_safe=True).apply({"flows": np.zeros(3)}, obs)
    assert action["override_qty"].sum() == pytest.approx(100)
    assert action["override_qty"][1] == 0


def test_queue_fleet_reservation_preserves_regular_routes_and_finishes_queued_cargo(module):
    policy = module.FuelRelease.__new__(module.FuelRelease)
    policy.fleet_caps = {"tb": 100}
    policy.flow_weights = np.asarray([1., 0., 2.])
    policy.release_weights = np.asarray([2., 0.])
    flows = np.asarray([100., 50., 10.])
    qty = np.asarray([40., 20.])
    out = policy._fleet_first({"flows": flows}, qty)
    assert out["flows"][1] == 50
    assert np.dot(out["flows"], policy.flow_weights) + np.dot(qty, policy.release_weights) == pytest.approx(100)
    np.testing.assert_array_equal(qty, [40, 20])
    np.testing.assert_array_equal(flows, [100, 50, 10])


def test_receiver_lp_origin_bonus_is_soft_preference_not_an_extra_resource(module):
    config, network, obs = release_case()
    obs["stock.qty"][[2, 3]] = [100, 0]
    weak = module.FuelRelease(config, network, power=1, receiver_cover=4, origin_bonus=1)
    strong = module.FuelRelease(config, network, power=1, receiver_cover=4, origin_bonus=4)
    redirected = weak.apply({"flows": np.zeros(3)}, obs)["override_qty"]
    preserved = strong.apply({"flows": np.zeros(3)}, obs)["override_qty"]
    np.testing.assert_allclose(redirected, [0, 100])
    np.testing.assert_allclose(preserved, [100, 0])
    obs["graph_now.kappa.tb"][:] = 30
    assert strong.apply({"flows": np.zeros(3)}, obs)["override_qty"].sum() == pytest.approx(30)


@pytest.mark.parametrize("value", [True, -1, float("nan"), "1"])
def test_origin_bonus_must_be_finite_nonnegative(module, value):
    config, network, _ = release_case()
    with pytest.raises(ValueError, match="origin_bonus"):
        module.FuelRelease(config, network, power=1, origin_bonus=value)


def test_origin_urgency_preserves_undercovered_recipient_without_inventing_resources(module):
    config, network, obs = release_case()
    obs["stock.qty"][[2, 3]] = [75, 0]
    weak = module.FuelRelease(config, network, power=1, receiver_cover=4, origin_bonus=1)
    adaptive = module.FuelRelease(config, network, power=1, receiver_cover=4, origin_bonus=1, origin_urgency=2)
    np.testing.assert_allclose(weak.apply({"flows": np.zeros(3)}, obs)["override_qty"], [0, 100])
    np.testing.assert_allclose(adaptive.apply({"flows": np.zeros(3)}, obs)["override_qty"], [100, 0])
    obs["graph_now.kappa.tb"][:] = 30
    assert adaptive.apply({"flows": np.zeros(3)}, obs)["override_qty"].sum() == pytest.approx(30)


def test_origin_urgency_does_not_read_hidden_coverage_padding(module):
    config, network, obs = release_case()
    obs["stock.qty.observed"][2] = 0
    policy = module.FuelRelease(config, network, power=1, receiver_cover=4, origin_urgency=4)
    expected = policy.apply({"flows": np.zeros(3)}, obs)["override_qty"]
    obs["stock.qty"][2] = np.nan
    np.testing.assert_array_equal(policy.apply({"flows": np.zeros(3)}, obs)["override_qty"], expected)


@pytest.mark.parametrize("value", [True, -1, float("nan"), "2"])
def test_origin_urgency_must_be_finite_nonnegative(module, value):
    config, network, _ = release_case()
    with pytest.raises(ValueError, match="origin_urgency"):
        module.FuelRelease(config, network, power=1, origin_urgency=value)
