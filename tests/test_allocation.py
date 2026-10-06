"""V4 regressions: shared stock/edge/fleet resources and explicit unmet needs."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from sbf_starter.agents import load
from tests.conftest import ROOT
from tests.test_current_network import hide_external, make_current_config, observation


@pytest.fixture
def api(monkeypatch):
    load(ROOT / "agents" / "team_agent")
    monkeypatch.syspath_prepend(str(ROOT / "agents" / "team_agent"))
    for name in ("allocation", "delivery", "queue_forecast"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    allocator = importlib.import_module("allocation").Allocator
    contracts = sys.modules["contracts"]
    cfg = make_current_config()
    cfg["static"]["edges"].update(alt_of=[None] * 10, mode=["sea"] * 10)
    cfg["static"]["lanes"]["alt_of"] = [None] * 3
    cfg["static"]["instance"]["params"] = {
        "fleet_share": {"tb": 1, "ct": 1},
        "fleet_measure": {"tb": 100, "ct": 100},
    }
    cfg["layout"]["stock_slots"] = [[0, 0], [3, 0], [0, 1]]

    def build(*, known_queue_work=True):
        obj = allocator(cfg)
        if known_queue_work:
            original = obj.delivery.options

            def options(*args, **kwargs):
                # Explicit no-queue fixture forecast, not a production default.
                return original(
                    *args, queue_work_at_arrival={(1, "tb"): 0, (2, "tb"): 0, (1, "ct"): 0, (2, "ct"): 0}, **kwargs
                )

            obj.delivery.options = options
        return obj

    return SimpleNamespace(Allocator=allocator, allocator=build, c=contracts, cfg=cfg)


def state(api, *, week=1, stock=20, source="observed", queues=(), pipeline=()):
    return SimpleNamespace(
        week=week,
        horizon=api.cfg["T"],
        available_stock={
            (0, 0): api.c.Quantity(stock, source),
            (3, 0): api.c.Quantity(7, "observed"),
            (0, 1): api.c.Quantity(5, "observed"),
        },
        queues=queues,
        pipeline=pipeline,
        issues=(),
        availability_mode="pre_dispatch_stock_t_minus_1",
    )


def need(id="one", destination=4, quantity=5, priority=3, due=8):
    return SimpleNamespace(
        need_id=id,
        destination_node=destination,
        commodity_id=0,
        quantity=quantity,
        priority=priority,
        due_week=due,
        reason="current_demand",
        shortage_cost_per_unit_usd=100,
        confidence=None,
    )


def test_priority_and_shared_entry_are_not_double_spent(api):
    high, low = (
        need("urgent", quantity=8, priority=4, due=12),
        need("later", destination=3, quantity=5, priority=1, due=12),
    )
    snapshot = state(api, stock=12)
    obs = observation(api.cfg)
    result = api.allocator().allocate(snapshot, [low, high], obs)
    np.testing.assert_array_equal(result.flows, [2, 8, 0, 0, 0])
    assert result.unmet_needs == (api.c.UnmetNeed("later", 3, "current_resources_exhausted_or_unknown"),)
    assert snapshot.available_stock[0, 0].value == 12
    assert obs["graph_now.u"][0] == 10
    assert next(u for u in result.resource_usage if u.kind == "stock").used == 10
    assert next(u for u in result.resource_usage if u.kind == "edge").used == 10


def test_exhausted_lane_falls_back_to_next_option(api):
    result = api.allocator().allocate(state(api), [need(quantity=12, due=12)], observation(api.cfg))
    assert result.flows[1] == 10 and result.flows[4] == 2
    assert not result.unmet_needs
    # Downstream edge's capacity is five, but it is not reserved this week.
    assert all(u.resource_index not in (1, 3) for u in result.resource_usage if u.kind == "edge")


def test_downstream_ban_is_excluded_before_stock_is_assigned(api):
    obs = observation(api.cfg)
    obs["graph_now.prohibited"][3, 0] = 1
    obs["action_mask"][1] = 0
    result = api.allocator().allocate(state(api), [need()], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5


def test_unknown_stock_is_not_spent_and_same_week_supply_is_not_added(api):
    snapshot = state(api, stock=None, source="unknown")
    snapshot.supply_availability = {(0, 0): api.c.Quantity(1000, "observed")}
    result = api.allocator().allocate(snapshot, [need()], observation(api.cfg))
    assert not np.any(result.flows)
    assert result.unmet_needs[0].remaining_quantity == 5
    assert snapshot.available_stock[0, 0].value is None


def test_blackout_does_not_establish_dispatch_permission(api):
    obs = observation(api.cfg)
    hide_external(obs)
    result = api.allocator().allocate(state(api), [need()], obs)
    assert not np.any(result.flows)
    assert "unconfirmed_permission" in result.unmet_needs[0].reason


def test_current_closure_is_skipped_for_open_alternative(api):
    obs = observation(api.cfg)
    obs["graph_now.open"][0] = obs["graph_now.kappa.tb"][0] = 0
    result = api.allocator().allocate(state(api), [need()], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5
    obs["graph_now.u"][6] = 0
    result = api.allocator().allocate(state(api), [need()], obs)
    assert not np.any(result.flows) and result.unmet_needs[0].remaining_quantity == 5
    assert "currently_blocked_delivery_route" in result.unmet_needs[0].reason


def detour(api):
    api.cfg["static"]["edges"]["alt_of"][6] = {"lane": 1}
    api.cfg["static"]["edges"]["tau0"][6] = 10  # Extra three fleet weeks per unit.
    api.cfg["static"]["instance"]["params"]["fleet_measure"]["tb"] = 6
    obs = observation(api.cfg)
    obs["graph_now.u"][0] = 0
    return obs


def test_detour_fleet_pool_is_shared_across_requests(api):
    obs = detour(api)
    result = api.allocator().allocate(state(api), [need("a", quantity=1), need("b", quantity=4)], obs)
    assert result.flows[4] == 2
    assert result.unmet_needs[0].need_id == "b" and result.unmet_needs[0].remaining_quantity == 3
    assert all(u.kind in ("stock", "edge", "chokepoint_pool") for u in result.resource_usage)
    assert any(r.code == "fleet_budget_usage" and "Used 6 of 6" in r.message for r in result.reasons)


def test_current_automatic_release_gets_a_fleet_bound(api):
    obs = detour(api)
    obs["pipeline.qty.observed"] = np.zeros(3, dtype=int)  # Empty padding, not a blackout.
    obs["queue_lots.qty.observed"] = np.array([1, 0, 0])
    api.cfg["static"]["edges"]["alt_of"][1] = {"edge": 2}
    queue = api.c.QueueLot("q", 1, 0, 0, "known", 1, 0, api.c.Quantity(4, "observed"))
    result = api.allocator().allocate(state(api, queues=(queue,)), [need()], obs)
    assert result.flows[4] == pytest.approx(2 / 3)
    assert any(r.code == "automatic_release_fleet_bound" for r in result.reasons)


def test_horizon_and_zero_time_direct_delivery(api):
    obs = observation(api.cfg, week=12)
    result = api.allocator().allocate(state(api, week=12), [need(due=12), need("direct", destination=5, due=12)], obs)
    assert result.flows[3] == 5 and result.flows[1] == result.flows[4] == 0
    assert result.unmet_needs[0].reason == "estimated_arrival_beyond_horizon"


def test_unknown_masked_capacity_uses_labelled_estimate(api):
    obs = observation(api.cfg)
    obs["graph_now.u"][0] = 0
    obs["graph_now.u.observed"][0] = 0
    result = api.allocator().allocate(state(api), [need()], obs)
    assert result.flows[1] == 5
    assert next(u for u in result.resource_usage if u.kind == "edge").limit_source == "estimated"


def test_incompatible_availability_order_is_rejected(api):
    snapshot = state(api)
    snapshot.availability_mode = "includes_same_week_supply"
    with pytest.raises(ValueError, match="pre-dispatch stock"):
        api.allocator().allocate(snapshot, [need()], observation(api.cfg))


def test_same_stock_on_different_edges_is_not_double_spent(api):
    obs = observation(api.cfg)
    obs["graph_now.prohibited"][3, 0] = 1
    obs["action_mask"][1] = 0
    result = api.allocator().allocate(
        state(api, stock=6), [need("a", quantity=5), need("b", destination=3, quantity=5)], obs
    )
    assert result.flows[4] == 5 and result.flows[0] == 1
    assert sum(result.flows) == 6 and result.unmet_needs[0].remaining_quantity == 4


def test_known_on_time_eta_beats_cheaper_late_route(api):
    result = api.allocator().allocate(state(api), [need(due=2)], observation(api.cfg))
    assert result.flows[4] == 5 and result.flows[1] == 0
    assert any(r.code == "eta_on_time_estimate" for r in result.reasons)


def test_unknown_eta_is_not_covered_by_nominal_transit(api):
    obs = observation(api.cfg)
    obs["graph_now.u"][6] = 0  # No direct alternative.
    result = api.allocator(known_queue_work=False).allocate(state(api), [need(due=12)], obs)
    assert not np.any(result.flows)
    assert result.unmet_needs[0].remaining_quantity == 5
    assert "delivery_eta_unknown" in result.unmet_needs[0].reason
    assert not any(r.code == "eta_on_time_estimate" for r in result.reasons)


def test_late_delivery_is_explicit_and_never_labelled_on_time(api):
    result = api.allocator().allocate(state(api), [need(due=1)], observation(api.cfg))
    assert result.flows[4] == 5
    assert any(r.code == "eta_late" for r in result.reasons)
    assert not any(r.code == "eta_on_time_estimate" for r in result.reasons)


def test_intermediate_movement_does_not_fulfil_final_destination_need(api):
    snapshot = state(api)
    snapshot.available_stock[3, 0] = api.c.Quantity(0, "observed")
    result = api.allocator().allocate(snapshot, [need(destination=5)], observation(api.cfg))
    assert not np.any(result.flows)
    assert result.unmet_needs[0].remaining_quantity == 5
    assert result.unmet_needs[0].reason == "current_resources_exhausted_or_unknown"


def test_no_route_and_default_release_are_explicit(api):
    result = api.allocator().allocate(state(api), [need(destination=6)], observation(api.cfg))
    assert not np.any(result.flows)
    assert result.unmet_needs == (api.c.UnmetNeed("one", 5, "no_permitted_delivery_slot"),)
    assert result.override_qty is None and result.release_mode is None


def test_repeated_same_week_and_permuted_need_order_are_deterministic(api):
    obj = api.allocator()
    obs, snapshot = observation(api.cfg), state(api, stock=6)
    first = obj.allocate(snapshot, [need("b", quantity=5), need("a", quantity=5)], obs)
    second = obj.allocate(snapshot, [need("a", quantity=5), need("b", quantity=5)], obs)
    np.testing.assert_array_equal(first.flows, second.flows)
    assert first.unmet_needs == second.unmet_needs
    assert first.resource_usage == second.resource_usage and first.reasons == second.reasons
    assert first.flows is not second.flows
    assert snapshot.available_stock[0, 0].value == 6
    obs["graph_now.c"][0] += 1
    with pytest.raises(ValueError, match="changed within"):
        obj.allocate(snapshot, [need()], obs)


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_real_requests_execute_without_clipping_in_nominal_week(api, task):
    import gymnasium as gym
    from shockbench_flow.dynamics.sim import initial_state, step
    from shockbench_flow.instance.io import load_instance
    from shockbench_flow.marks import event_free_marks
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id

    env = gym.make(env_id(task), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        cfg = agent_config_from_reset(env, obs, info)
    finally:
        env.close()
    allocator = api.Allocator(cfg)
    for key, value in allocator.tracker.nominal.items():
        obs[key] = np.nan_to_num(value.copy())
        obs[key + ".observed"] = np.ones(1, dtype=int) if key == "action_mask" else np.isfinite(value)
    builder = importlib.import_module("state").StateBuilder(cfg)
    planner = importlib.import_module("needs").NeedPlanner(cfg)
    snapshot = builder.build(obs, allocator.network)
    requests = planner.plan(snapshot, obs, allocator.network)
    result = allocator.allocate(snapshot, requests, obs)
    assert np.any(result.flows)
    inst = load_instance(cfg["static"]["instance"])
    record = step(
        inst,
        event_free_marks(inst),
        initial_state(inst),
        {s: float(qty) for s, qty in enumerate(result.flows) if qty > 0},
    )
    for slot, requested in record.requested.items():
        assert record.executed[slot] == pytest.approx(requested, rel=1e-9, abs=1e-9)


def test_opt_in_packed_agent_runs_its_real_pipeline(tmp_path):
    import json
    from zipfile import ZipFile

    import gymnasium as gym
    from shockbench_flow_agent.submission import build_submission, check_zip, missing_imports
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id

    source = ROOT / "agents" / "team_agent"
    archive = build_submission(source, tmp_path / "candidate.zip")
    checked = check_zip(archive)
    assert "allocation.py" in {name for name, _ in checked.files}
    folder = tmp_path / "unpacked"
    with ZipFile(archive) as zipped:
        zipped.extractall(folder)
    (folder / "params.json").write_text(json.dumps({"allocation_enabled": True}), encoding="utf-8")
    files = [p.relative_to(folder).as_posix() for p in folder.rglob("*.py")]
    for name in files:
        assert missing_imports((folder / name).read_bytes(), files) == []
    env = gym.make(env_id("small"), entropy=12345)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        config = agent_config_from_reset(env, obs, info)
        agent = load(folder)(config)
        action = agent.act(obs)
        assert agent.last_allocation is not None
        assert env.action_space.contains(
            action
            | {
                "override_qty": np.zeros(tuple(config["spaces"]["action"]["override_qty"]["shape"])),
                "release_mode": np.zeros(tuple(config["spaces"]["action"]["release_mode"]["shape"]), dtype=np.int64),
            }
        )
    finally:
        env.close()
