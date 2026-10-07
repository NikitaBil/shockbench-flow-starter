"""V5.2: observed closure end dates, FIFO waiting, and economic alternatives."""

import importlib

import numpy as np
import pytest

from tests.test_allocation import api as api
from tests.test_allocation import need, queue_observation, state


def closures(api, rows=((2, 6),), *, nodes=(2,)):
    obs = queue_observation(api)
    positions = {node: p for p, node in enumerate(api.cfg["layout"]["chokepoints"])}
    for node in nodes:
        pos = positions[node]
        obs["graph_now.open"][pos] = 0
        obs["graph_now.kappa.tb"][pos] = obs["graph_now.kappa.ct"][pos] = 0
    for i, field in enumerate(("chokepoint", "end_week")):
        key = f"closure_end.{field}"
        obs[key] = np.array([row[i] for row in rows] + [0], dtype=np.int64)
        obs[key + ".observed"] = np.array([1] * len(rows) + [0], dtype=np.int8)
    return obs


def allocator(api, *, enabled=True):
    return api.Allocator(api.cfg, queue_eta_enabled=True, closure_wait_enabled=enabled)


def test_relaxed_deadline_chooses_cheaper_queue_wait(api):
    result = allocator(api).allocate(state(api), [need(due=12)], closures(api))
    assert result.flows[1] == 5 and result.flows[4] == 0
    assert any(r.code == "wait_for_announced_reopening" and "completion 10" in r.message for r in result.reasons)
    assert any(r.code == "eta_on_time_estimate" and r.slot_id == 1 for r in result.reasons)
    assert sum(u.used for u in result.resource_usage if u.kind == "stock") == 5
    assert result.override_qty is None and result.release_mode is None


def test_urgent_deadline_chooses_timely_detour(api):
    result = allocator(api).allocate(state(api), [need(due=8)], closures(api))
    assert result.flows[1] == 0 and result.flows[4] == 5
    assert not any(r.code == "wait_for_announced_reopening" for r in result.reasons)


def test_reopening_before_arrival_does_not_add_waiting_weeks(api):
    result = allocator(api).allocate(state(api), [need(due=8)], closures(api, rows=((2, 3),)))
    assert result.flows[1] == 5
    assert any(r.code == "queue_eta_estimate" and "completion week 8" in r.message for r in result.reasons)


def test_effective_reopening_week_allows_release_in_that_week(api):
    result = allocator(api).allocate(state(api), [need(due=8)], closures(api, rows=((2, 4),)))
    assert result.flows[1] == 5
    assert any(r.code == "queue_eta_estimate" and "completion week 8" in r.message for r in result.reasons)


def test_multiple_overlapping_closures_use_latest_end_deterministically(api):
    rows = ((2, 6), (2, 8))
    first = allocator(api).allocate(state(api), [need(due=12)], closures(api, rows))
    second = allocator(api).allocate(state(api), [need(due=12)], closures(api, rows[::-1]))
    np.testing.assert_array_equal(first.flows, second.flows)
    assert first.reasons == second.reasons
    assert any(r.code == "wait_for_announced_reopening" and "completion 12" in r.message for r in first.reasons)


@pytest.mark.parametrize("hidden", ["end_week", "chokepoint", "openness", "throughput"])
def test_unknown_closure_information_does_not_invent_reopening(api, hidden):
    obs = closures(api)
    if hidden in ("end_week", "chokepoint"):
        obs[f"closure_end.{hidden}.observed"][0] = 0
        obs[f"closure_end.{hidden}"][0] = -999
    else:
        key = "graph_now.open" if hidden == "openness" else "graph_now.kappa.tb"
        obs[key + ".observed"][0] = 0
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5


def test_one_hidden_overlapping_end_disables_waiting_for_that_node(api):
    obs = closures(api, rows=((2, 6), (2, 8)))
    obs["closure_end.end_week.observed"][1] = 0
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5


def test_both_closed_chokepoints_need_known_ends(api):
    obj = allocator(api)
    result = obj.allocate(state(api), [need(due=12)], closures(api, nodes=(1, 2)))
    assert result.flows[1] == 0 and result.flows[4] == 5
    obj = allocator(api)
    result = obj.allocate(state(api), [need(due=12)], closures(api, rows=((1, 3), (2, 6)), nodes=(1, 2)))
    assert result.flows[1] == 5
    assert any(r.code == "wait_for_announced_reopening" and "completion 10" in r.message for r in result.reasons)


def test_fifo_backlog_can_make_reopened_route_too_late(api):
    old = api.c.QueueLot("old", 2, 0, 1, "known", 3, 0, api.c.Quantity(20, "observed"))
    result = allocator(api).allocate(state(api, queues=(old,)), [need(due=12)], closures(api))
    assert result.flows[1] == 0 and result.flows[4] == 5


def test_batch_releases_are_forecast_in_full_not_from_first_release(api):
    result = allocator(api).allocate(state(api), [need(quantity=8, due=12)], closures(api, rows=((2, 7),)))
    assert result.flows[1] == 8
    assert any(r.code == "queue_eta_estimate" and "completion week 12" in r.message for r in result.reasons)


def test_waiting_cost_can_exceed_detour_premium(api):
    api.cfg["static"]["instance"]["nodes"][2]["chokepoint"]["queue_holding"] = {"fuel": [3, 3, 3]}
    # Sea freight 7 + upper-bound queue holding 2*3 exceeds air freight 12.
    result = allocator(api).allocate(state(api), [need(due=12)], closures(api))
    assert result.flows[1] == 0 and result.flows[4] == 5


@pytest.mark.parametrize("blocked", ["stock", "entry", "downstream_capacity", "ban"])
def test_waiting_does_not_rescue_missing_current_resources(api, blocked):
    obs, snapshot = closures(api), state(api)
    obs["graph_now.u"][6] = 0
    if blocked == "stock":
        snapshot.available_stock[0, 0] = api.c.Quantity(0, "observed")
    elif blocked == "entry":
        obs["graph_now.u"][0] = 0
    elif blocked == "downstream_capacity":
        obs["graph_now.u"][3] = 0
    else:
        obs["graph_now.prohibited"][3, 0] = 1
        obs["action_mask"][1] = 0
    obj = allocator(api)
    obj.queue_forecaster.forecast = lambda *args, **kwargs: pytest.fail("infeasible candidate consumed ETA budget")
    result = obj.allocate(snapshot, [need(due=12)], obs)
    assert not np.any(result.flows)
    assert any("Ran 0/16" in r.message for r in result.reasons if r.code == "queue_eta_forecast_usage")


def test_reopening_outside_forecast_window_keeps_eta_unknown(api, monkeypatch):
    monkeypatch.setattr(importlib.import_module("delivery_eta").CandidateETA, "MAX_WEEKS", 3)
    obs = closures(api)
    obs["graph_now.u"][6] = 0
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert not np.any(result.flows)
    assert "delivery_eta_unknown" in result.unmet_needs[0].reason
    assert not any(r.code == "eta_on_time_estimate" for r in result.reasons)


@pytest.mark.parametrize("end", [11, 20])
def test_closure_plus_transit_beyond_horizon_is_pruned_without_forecast(api, end):
    obs = closures(api, rows=((2, end),))
    obs["graph_now.u"][6] = 0
    obj = allocator(api)
    obj.queue_forecaster.forecast = lambda *args, **kwargs: pytest.fail("known episode bound consumed ETA budget")
    result = obj.allocate(state(api), [need(due=12)], obs)
    assert not np.any(result.flows)
    assert "estimated_arrival_beyond_horizon" in result.unmet_needs[0].reason


def test_shared_stock_and_need_order_remain_deterministic(api):
    requests = [need("quiet", quantity=5, priority=1, due=12), need("urgent", quantity=3, priority=4, due=8)]
    first = allocator(api).allocate(state(api, stock=7), requests, closures(api))
    second = allocator(api).allocate(state(api, stock=7), requests[::-1], closures(api))
    np.testing.assert_array_equal(first.flows, second.flows)
    assert first.reasons == second.reasons
    assert first.flows[4] == 3 and first.flows[1] == 4
    assert first.unmet_needs[0].need_id == "quiet" and first.unmet_needs[0].remaining_quantity == 1


def test_waiting_addition_cannot_delay_selected_higher_priority_eta(api):
    requests = [need("high", quantity=2, priority=5, due=10), need("low", quantity=8, priority=1, due=12)]
    result = allocator(api).allocate(state(api), requests, closures(api))
    assert result.flows[1] == 2 and result.flows[4] == 8
    assert not result.unmet_needs
    assert any("queue_eta_delays_selected_shipment" in r.message for r in result.reasons)


def test_announced_ban_guard_uses_release_after_waiting(api):
    from tests.test_announced_delivery_eta import announced

    obs = announced(closures(api), [(3, 0, 5)])
    obj = api.Allocator(api.cfg, queue_eta_enabled=True, closure_wait_enabled=True, announced_eta_guard_enabled=True)
    result = obj.allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5
    assert any("queue_eta_announced_prohibition_conflict" in r.message for r in result.reasons)


def test_disabled_flag_preserves_closure_skip(api):
    result = allocator(api, enabled=False).allocate(state(api), [need(due=12)], closures(api))
    assert result.flows[1] == 0 and result.flows[4] == 5


def test_partial_closure_recovers_nominal_pool_rate_without_double_openness(api):
    obs = closures(api, rows=((2, 4),))
    obs["graph_now.open"][0] = 0.5
    obs["graph_now.kappa.tb"][0] = 5
    obs["graph_now.u"][3] = 20
    result = allocator(api).allocate(state(api), [need(quantity=8, due=8)], obs)
    assert result.flows[1] == 8
    assert any(r.code == "queue_eta_estimate" and "completion week 8" in r.message for r in result.reasons)


def test_flag_requires_queue_eta_and_boolean(api):
    with pytest.raises(ValueError, match="requires queue_eta_enabled"):
        api.Allocator(api.cfg, closure_wait_enabled=True)
    with pytest.raises(ValueError, match="must be a boolean"):
        api.Allocator(api.cfg, queue_eta_enabled=True, closure_wait_enabled="true")


def test_standard_regime_hides_closure_end_dates():
    from shockbench_flow.information.theta import resolve_regime

    # A benchmark update changing this assumption requires new evaluation.
    assert resolve_regime("standard").chi is False


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_packed_opt_in_agent_runs_on_real_shapes(tmp_path, task):
    import json
    from zipfile import ZipFile

    import gymnasium as gym
    from shockbench_flow_agent.submission import build_submission, check_zip, missing_imports
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id
    from sbf_starter.agents import load
    from tests.conftest import ROOT

    archive = build_submission(ROOT / "agents" / "team_agent", tmp_path / "wait.zip")
    checked = check_zip(archive)
    assert "delivery_closure.py" in {name for name, _ in checked.files}
    folder = tmp_path / "unpacked"
    with ZipFile(archive) as zipped:
        zipped.extractall(folder)
    (folder / "params.json").write_text(
        json.dumps(
            {
                "allocation_enabled": True,
                "queue_eta_enabled": True,
                "closure_wait_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    files = [p.relative_to(folder).as_posix() for p in folder.rglob("*.py")]
    for name in files:
        assert missing_imports((folder / name).read_bytes(), files) == []
    env = gym.make(env_id(task), entropy=67890)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        agent = load(folder)(agent_config_from_reset(env, obs, info))
        assert agent.pipeline.allocator.closure_wait_enabled is True
        action = agent.act(obs)
        assert np.all(np.isfinite(action["flows"])) and np.all(action["flows"] >= 0)
        assert env.action_space.contains(
            action
            | {
                "override_qty": np.zeros(env.action_space["override_qty"].shape),
                "release_mode": np.zeros(env.action_space["release_mode"].shape, dtype=np.int64),
            }
        )
        env.step(action)
    finally:
        env.close()
