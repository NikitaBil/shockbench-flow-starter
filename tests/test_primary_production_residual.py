"""Candidate residual allocation: current resources and exact control parity."""

from types import SimpleNamespace

import gymnasium as gym
import numpy as np
import pytest
from shockbench_flow_gym import agent_config_from_reset

from sbf_starter import env_id
from sbf_starter.agents import load
from tests.conftest import ROOT


CANDIDATE = ROOT / "agents/team_agent_vitya_v1"
PRIMARY = ROOT / "agents/team_agent"


def residual_case():
    cls = load(CANDIDATE).__init__.__globals__["ProductionResidual"]
    helper = cls.__new__(cls)
    routes = tuple(SimpleNamespace(slot_id=i, source_node=source, destination_node=target,
                                   commodity_id=0, edge_id=i // 2, edges=(i // 2,), lane_id=None,
                                   nominal_freight_per_unit=1)
                   for i, (source, target) in enumerate(((0, 2), (1, 2), (0, 3), (1, 3))))
    network = SimpleNamespace(routes=routes, slots_from={(0, 0): (0, 2), (1, 0): (1, 3)},
                              slots_to={(2, 0): (0, 1), (3, 0): (2, 3)}, edge_transit_weeks=(1, 1))
    targets = ((2, 0), (3, 0))
    model = SimpleNamespace(target_rates={pair: ("graph_now.osat.thr_eff", i, 1) for i, pair in enumerate(targets)},
                            nominal={pair: 10 for pair in targets}, penalties={pair: 100 for pair in targets},
                            production_times={pair: 1 for pair in targets}, goods_value=[1], _arrivals=lambda obs: {})
    helper.enabled, helper.network, helper.models, helper.slots = True, network, (model,), tuple(range(4))
    helper.targets, helper.stocks = dict.fromkeys(targets, model), {(i, 0): i for i in range(4)}
    helper.entries, helper.weights, helper.output_stocks = {0: [0, 1], 1: [2, 3]}, np.zeros(4), {}
    obs = {"week": np.array([2]), "stock.qty": np.array([10., 10., 0., 0.]),
           "stock.qty.observed": np.ones(4), "graph_now.u": np.array([10., 10.]),
           "graph_now.u.observed": np.ones(2), "action_mask": np.ones(4),
           "graph_now.prohibited": np.zeros((2, 1)), "graph_now.prohibited.observed": np.ones((2, 1)),
           "graph_now.tau": np.ones(2), "graph_now.tau.observed": np.ones(2),
           "graph_now.c": np.ones(2), "graph_now.c.observed": np.ones(2),
           "graph_now.tariff": np.zeros((2, 1)), "graph_now.tariff.observed": np.ones((2, 1)),
           "graph_now.osat.thr_eff": np.full(2, 10.), "graph_now.osat.thr_eff.observed": np.ones(2)}
    return helper, obs, np.ones(4), np.full(4, 10.)


def test_shared_stock_and_entry_capacity_are_counted_once():
    helper, obs, flows, preferred = residual_case()
    out = helper.apply(flows, preferred, obs)
    assert out.sum() > flows.sum()
    for ids in ([0, 2], [1, 3], [0, 1], [2, 3]):
        assert out[ids].sum() <= 10 + 1e-9
    assert np.all(out >= flows) and np.all(out <= preferred)
    np.testing.assert_array_equal(flows, np.ones(4))
    np.testing.assert_array_equal(out, helper.apply(flows, preferred, obs))


def test_receiver_budget_is_shared_across_sources():
    helper, obs, flows, preferred = residual_case()
    obs["stock.qty"][2] = 16
    out = helper.apply(flows, preferred, obs)
    assert out[:2].sum() <= 4 + 1e-9  # 10 * (transit 1 + cover 1) - stock 16


def test_inbound_source_stock_is_not_dispatchable_and_target_arrivals_are_credited():
    helper, obs, flows, requests = residual_case()
    helper.models[0]._arrivals = lambda obs: {(0, 0): [(2, 1000)], (2, 0): [(2, 18)]}
    out = helper.apply(flows, requests, obs)
    assert out[[0, 2]].sum() <= 10 + 1e-9  # source receipt this week is not in I_prev
    np.testing.assert_array_equal(out[:2], flows[:2])  # target production can use this week's arrival


def test_candidate_queue_release_keeps_arrivals_in_pre_release_phase():
    from tests.test_fuel_release import release_case

    config, network, obs = release_case()
    release_class = load(CANDIDATE).__init__.__globals__["FuelRelease"]
    network.edge_head = (0, 2, 3)
    obs["pipeline.qty"] = np.array([25.])
    obs["pipeline.qty.observed"] = np.ones(1)
    obs["pipeline.lane"] = np.zeros(1, dtype=int)
    obs["pipeline.lane.observed"] = np.zeros(1, dtype=int)
    for field, value in (("edge", 0), ("k", 0), ("arrival_week", 2)):
        obs[f"pipeline.{field}"] = np.array([value])
        obs[f"pipeline.{field}.observed"] = np.ones(1)
    obs["graph_now.kappa.tb"][:] = 200
    obs["graph_now.u"][:] = 200
    action = release_class(config, network, power=2).apply({"flows": np.zeros(3)}, obs)
    assert action["override_qty"].sum() == pytest.approx(125)


@pytest.mark.parametrize("field", ["stock.qty", "graph_now.u"])
def test_hidden_padding_is_not_a_resource(field):
    helper, obs, flows, preferred = residual_case()
    obs[field][:] = np.nan
    obs[field + ".observed"][:] = 0
    assert helper.apply(flows, preferred, obs) is flows


def test_ban_zero_capacity_and_action_mask_prevent_additions():
    helper, obs, flows, preferred = residual_case()
    obs["graph_now.prohibited"][0, 0] = 1
    out = helper.apply(flows, preferred, obs)
    np.testing.assert_array_equal(out[:2], flows[:2])
    obs["graph_now.prohibited"][0, 0] = 0
    obs["graph_now.u"][0] = 0
    out = helper.apply(flows, preferred, obs)
    np.testing.assert_array_equal(out[:2], flows[:2])
    obs["action_mask"][:] = 0
    assert helper.apply(flows, preferred, obs) is flows


def test_unknown_transit_uses_estimate_without_zeroing_existing_dispatch():
    helper, obs, flows, preferred = residual_case()
    obs["graph_now.tau"][:] = np.nan
    obs["graph_now.tau.observed"][:] = 0
    out = helper.apply(flows, preferred, obs)
    assert np.all(out >= flows) and np.isfinite(out).all()
    assert helper.last["eta_status"] == "conditional_estimate"


def test_filtered_requests_are_the_bound_and_explicit_zero_is_not_restored():
    helper, obs, flows, preferred = residual_case()
    preferred[:] = flows
    assert helper.apply(flows, preferred, obs) is flows
    requests = np.full(4, 10.)
    preferred[:] = 0
    assert helper.apply(flows, requests, obs, preferred=preferred) is flows


def test_suppression_is_relaxed_only_with_current_stock_capacity_and_receiver_room():
    helper, obs, flows, requests = residual_case()
    preferred = np.full(4, 2.)
    out = helper.apply(flows, requests, obs, preferred=preferred)
    assert np.any(out > preferred)
    for ids in ([0, 2], [1, 3], [0, 1], [2, 3]):
        assert out[ids].sum() <= 10 + 1e-9
    assert np.all(out <= requests)


def test_relative_preference_ranks_extra_requests():
    helper, obs, flows, requests = residual_case()
    preferred = np.array([10., 1., 10., 1.])
    obs["stock.qty"][:2] = 100
    out = helper.apply(flows, requests, obs, preferred=preferred)
    assert out[0] > out[1] and out[2] > out[3]


def test_duplicate_fleet_weights_are_quantity_times_weeks():
    helper, obs, flows, preferred = residual_case()
    helper.weights[:] = [2, 3, 0, 0]
    out = helper.apply(flows, preferred, obs)
    np.testing.assert_array_equal(out[:2], flows[:2])
    assert out[2:].sum() > flows[2:].sum()
    assert float(helper.weights @ (out - flows)) == 0  # no unbudgeted shared fleet usage


def test_full_output_storage_prevents_extra_inputs():
    helper, obs, flows, preferred = residual_case()
    helper.output_stocks[2, 0] = ((2, 0), 1)
    obs["stock.qty"][2] = 1
    out = helper.apply(flows, preferred, obs)
    np.testing.assert_array_equal(out[:2], flows[:2])


def test_solver_timeout_retains_control(monkeypatch):
    import scipy.optimize

    helper, obs, flows, preferred = residual_case()
    monkeypatch.setattr(scipy.optimize, "linprog", lambda *a, **k: SimpleNamespace(success=False, status=1))
    assert helper.apply(flows, preferred, obs) is flows
    assert helper.last["status"] == "solver_control_fallback"


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_flag_off_matches_champion_every_week(task):
    env = gym.make(env_id(task), entropy=202610081)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        champion = load(PRIMARY)(config)
        candidate_class = load(CANDIDATE)
        params = candidate_class.__init__.__globals__["PARAMS"]
        params["production_residual_enabled"] = False
        candidate = candidate_class(config)
        assert candidate.pipeline is None
        done = False
        while not done:
            actual, expected = candidate.act(obs), champion.act(obs)
            assert actual.keys() == expected.keys()
            for key in actual:
                np.testing.assert_array_equal(actual[key], expected[key])
                assert env.action_space[key].contains(actual[key])
            obs, _, terminated, truncated, _ = env.step(expected)
            done = terminated or truncated
    finally:
        env.close()


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_enabled_preserves_fuel_pulses_release_and_valid_output(task):
    env = gym.make(env_id(task), entropy=202610081)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        champion = load(PRIMARY)(config)
        candidate = load(CANDIDATE)(config)
        fuel_ids = [r.slot_id for r in candidate.network.routes
                    if candidate.network.commodity_names[r.commodity_id] in ("lng", "crude", "nucfuel")]
        for _ in range(7):
            before = {k: v.copy() for k, v in obs.items()}
            actual, expected = candidate.act(obs), champion.act(obs)
            np.testing.assert_array_equal(actual["flows"][fuel_ids], expected["flows"][fuel_ids])
            for key in ("override_qty", "release_mode"):
                if key in expected:
                    np.testing.assert_array_equal(actual[key], expected[key])
            for key in actual:
                assert env.action_space[key].contains(actual[key])
                assert np.isfinite(actual[key]).all() and np.all(actual[key] >= 0)
            assert not np.any(actual["flows"][~obs["action_mask"].astype(bool)])
            for k, v in obs.items():
                np.testing.assert_array_equal(v, before[k])
            obs, _, _, _, _ = env.step(expected)
    finally:
        env.close()
