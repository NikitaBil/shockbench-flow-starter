"""V3: full-route transport cost, arrival timing and explicit uncertainties."""

import sys

import numpy as np
import pytest

from agents.team_agent.delivery import DeliveryEvaluator
from agents.team_agent.network import NetworkTracker
from tests.test_current_network import hide_external, make_current_config, observation


@pytest.fixture
def config():
    return make_current_config()


def options(cfg, obs, **kwargs):
    tracker = NetworkTracker(cfg)
    evaluator = DeliveryEvaluator(cfg, tracker.network)
    return evaluator.options(tracker.update(obs), destination_node=4, commodity_id=0, **kwargs)


def test_full_lane_freight_ad_valorem_tariffs_and_war_risk(config):
    obs = observation(config)
    obs["graph_now.tariff"][0, 0] = 0.1
    obs["graph_now.tariff"][1, 0] = 0.2
    obs["graph_now.war_risk"][:] = [2, 1]  # Node order is not observation order.
    result = options(config, obs, quantity=2, queue_work_at_arrival={(1, "tb"): 0, (2, "tb"): 0})
    lane = next(option for option in result if option.slot_id == 1)
    assert lane.freight_per_unit == 7
    assert lane.tariff_per_unit == pytest.approx(30)  # (0.1 + 0.2) * customs value 100.
    assert lane.war_risk_per_unit == 18  # 7 at first chokepoint + 11 at second.
    assert lane.transport_cost_per_unit == pytest.approx(55)
    assert lane.transport_cost == pytest.approx(110)
    assert lane.cost_observed
    assert result[0].slot_id == 4  # Air is cheaper after lane tariff/premium.


def test_no_extra_week_per_chokepoint_and_direct_zero_transit(config):
    obs = observation(config)
    result = options(config, obs, queue_work_at_arrival={(1, "tb"): 0, (2, "tb"): 0}, due_week=2)
    lane = next(option for option in result if option.slot_id == 1)
    air = next(option for option in result if option.slot_id == 4)
    assert lane.no_wait_arrival_week == 8  # week 1 + edge times 1 + 2 + 4.
    assert lane.estimated_completion_week == 8 and lane.late_weeks == 6
    assert air.estimated_completion_week == 2 and air.late_weeks == 0
    state = NetworkTracker(config).update(obs)
    direct = DeliveryEvaluator(config).options(state, destination_node=5, commodity_id=0)[0]
    assert direct.no_wait_arrival_week == 1 and direct.estimated_completion_week == 1


def test_downstream_sanction_is_excluded_before_comparing_alternatives(config):
    obs = observation(config)
    obs["graph_now.prohibited"][3, 0] = 1
    obs["action_mask"][1] = 0
    assert [option.slot_id for option in options(config, obs)] == [4]


def test_unknown_queues_and_current_closure_do_not_invent_reopening(config):
    obs = observation(config)
    lane = next(option for option in options(config, obs) if option.slot_id == 1)
    assert lane.estimated_completion_week is None and "queue_work_unknown" in lane.delay_flags
    obs["graph_now.open"][0] = 0
    obs["graph_now.kappa.tb"][0] = 0
    lane = next(
        option
        for option in options(config, obs, queue_work_at_arrival={(1, "tb"): 0, (2, "tb"): 0})
        if option.slot_id == 1
    )
    assert lane.dispatchable_now  # Legal entry to lane; downstream cargo will wait.
    assert lane.estimated_completion_week is None
    assert "currently_closed_chokepoint" in lane.delay_flags
    assert "zero_chokepoint_throughput" in lane.delay_flags


def test_quantity_batching_and_queue_work_are_labelled_estimates(config):
    obs = observation(config)
    queues = {(1, "tb"): 19, (2, "tb"): 0}
    lane = next(
        option
        for option in options(config, obs, quantity=2, queue_work_at_arrival=queues, due_week=8)
        if option.slot_id == 1
    )
    assert lane.queue_delay_weeks == 2 and lane.estimated_completion_week == 10
    assert lane.late_weeks == 2 and "queue_delay_estimate" in lane.delay_flags
    lane = next(
        option
        for option in options(config, obs, quantity=10, queue_work_at_arrival={(1, "tb"): 0, (2, "tb"): 0})
        if option.slot_id == 1
    )
    assert lane.snapshot_throughput == 5 and lane.estimated_completion_week == 9
    assert "multiple_dispatch_or_service_weeks" in lane.delay_flags


def test_blackout_labels_cost_and_permission_as_estimates(config):
    obs = observation(config)
    hide_external(obs)
    lane = next(option for option in options(config, obs) if option.slot_id == 1)
    assert not lane.cost_observed and not lane.permission_observed
    assert "unconfirmed_permission" in lane.delay_flags
    assert lane.transport_cost_per_unit == 7


def test_source_filter_and_end_of_episode_are_explicit(config):
    obs = observation(config, week=12)
    result = options(config, obs, source_nodes=[0], due_week=12)
    assert all(option.beyond_horizon for option in result)
    assert options(config, obs, source_nodes=[3]) == ()
    state = NetworkTracker(config).update(obs)
    direct = DeliveryEvaluator(config).options(state, 5, 0)[0]
    assert not direct.beyond_horizon


def test_invalid_quantity_or_queue_work_is_rejected(config):
    with pytest.raises(ValueError, match="quantity"):
        options(config, observation(config), quantity=np.inf)
    with pytest.raises(ValueError, match="queue work"):
        options(config, observation(config), queue_work_at_arrival={(1, "tb"): -1, (2, "tb"): 0})


def test_helpers_import_as_top_level_submission_modules(monkeypatch):
    import importlib.util
    from pathlib import Path

    folder = Path(__file__).resolve().parents[1] / "agents" / "team_agent"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("delivery_submission", folder / "delivery.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    assert module.DeliveryEvaluator(make_current_config()).network.routes


def test_no_wait_timing_against_native_simulator():
    import gymnasium as gym
    from shockbench_flow.dynamics.sim import initial_state, step
    from shockbench_flow.instance.io import load_instance
    from shockbench_flow.marks import event_free_marks
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id

    env = gym.make(env_id("small"), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        cfg = agent_config_from_reset(env, obs, info)
    finally:
        env.close()
    instance = load_instance(cfg["static"]["instance"])
    marks = event_free_marks(instance)
    tracker = NetworkTracker(cfg)
    nominal_obs = {"week": np.array([1], dtype=int)}
    for key, values in tracker.nominal.items():
        nominal_obs[key] = values.copy()
        nominal_obs[key + ".observed"] = np.ones(1, dtype=int) if key == "action_mask" else np.isfinite(values)
    snapshot = tracker.update(nominal_obs)
    evaluator = DeliveryEvaluator(cfg, tracker.network)
    routes = tracker.network.routes
    probes = [
        next(r for r in routes if r.lane_id is not None and snapshot.routes[r.slot_id].sanction_allowed),
        next(r for r in routes if r.lane_id is None and r.nominal_transit_weeks == 0),
    ]
    for route in probes:
        result = evaluator.options(
            snapshot,
            route.destination_node,
            route.commodity_id,
            queue_work_at_arrival={(c, route.pool): 0 for c in route.chokepoints},
        )
        option = next(o for o in result if o.slot_id == route.slot_id)
        state = initial_state(instance)
        # Isolate one unit of cargo from reset queues and historical shipments.
        state.pipeline.clear()
        state.lots.clear()
        for s, slot in enumerate(instance.stock_slots):
            if slot.node in instance.chokepoints:
                state.stock[s] = 0
        state.stock[instance.slot_index[route.source_node, route.commodity_id]] = 1
        record = step(instance, marks, state, {route.slot_id: 1})
        assert record.executed[route.slot_id] == 1
        while state.pipeline or state.lots:
            step(instance, marks, state, {})
        assert state.week == option.no_wait_arrival_week == option.estimated_completion_week
