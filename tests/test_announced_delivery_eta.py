"""V5 experiment: noisy announcements invalidate conflicting conditional ETAs."""

import numpy as np
import pytest

from tests.test_allocation import api as api
from tests.test_allocation import need, queue_observation, state


def announced(obs, rows):
    for index, name in enumerate(("edge", "k", "effective_week")):
        key = f"pending_prohibitions.{name}"
        obs[key] = np.array([row[index] for row in rows] + [0], dtype=np.int64)
        obs[key + ".observed"] = np.array([1] * len(rows) + [0], dtype=np.int8)
    return obs


def allocator(api, *, enabled=True):
    return api.Allocator(api.cfg, queue_eta_enabled=True, announced_eta_guard_enabled=enabled)


@pytest.mark.parametrize("edge,effective", [(1, 2), (3, 4)])
def test_no_wait_conflict_does_not_spend_forecast_budget(api, edge, effective):
    obs = announced(queue_observation(api), [(edge, 0, effective)])
    obj = allocator(api)
    obj.queue_forecaster.forecast = lambda *args, **kwargs: pytest.fail("cheap conflict should skip forecast")
    result = obj.allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5
    assert any("Ran 0/16" in r.message for r in result.reasons if r.code == "queue_eta_forecast_usage")


@pytest.mark.parametrize("edge,commodity,effective", [(0, 0, 2), (3, 0, 5), (3, 1, 2), (2, 0, 2)])
def test_already_entered_or_unaffected_legs_keep_valid_eta(api, edge, commodity, effective):
    obs = announced(queue_observation(api), [(edge, commodity, effective)])
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 5 and result.flows[4] == 0
    assert any(r.code == "queue_eta_estimate" and "completion week 8" in r.message for r in result.reasons)


def test_queue_delay_crosses_ban_even_when_no_wait_entry_precedes_it(api):
    obs = announced(queue_observation(api), [(3, 0, 5)])
    obj = allocator(api)
    trace = []
    obj.trace_callback = trace.append
    result = obj.allocate(state(api), [need(quantity=8, due=12)], obs)
    assert result.flows[1] == 0 and result.flows[4] == 8
    rejected = next(row for row in trace if row["stage"] == "eta" and row["slot_id"] == 1)
    assert rejected["reason"] == "queue_eta_announced_prohibition_conflict"
    assert rejected["eta"] is None and rejected["calls_after"] == 1
    assert not any(r.code == "eta_on_time_estimate" and r.slot_id == 1 for r in result.reasons)


def test_conflicting_competitor_invalidates_joint_forecast(api):
    obs = announced(queue_observation(api), [(2, 0, 2)])
    obs["graph_now.u"][2] = 1
    old = api.c.QueueLot("old", 2, 0, 0, "known", 2, 0, api.c.Quantity(7, "observed"))
    result = allocator(api).allocate(state(api, queues=(old,)), [need(due=12)], obs)
    assert result.flows[1] == 0 and result.flows[4] == 5
    assert any("queue_eta_announced_prohibition_conflict" in r.message for r in result.reasons)


@pytest.mark.parametrize("hidden", ["edge", "k", "effective_week"])
def test_masked_row_and_padding_are_not_interpreted_as_announcements(api, hidden):
    obs = announced(queue_observation(api), [(1, 0, 2)])
    obs[f"pending_prohibitions.{hidden}.observed"][0] = 0
    obs[f"pending_prohibitions.{hidden}"][0] = -999
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 5


def test_unobserved_transit_cannot_establish_cheap_conflict(api):
    obs = announced(queue_observation(api), [(3, 0, 2)])
    obs["graph_now.tau.observed"][0] = 0
    obs["graph_now.u"][6] = 0
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert not np.any(result.flows)
    assert "queue_eta_transit_unknown" in result.unmet_needs[0].reason
    assert "queue_eta_announced_prohibition_conflict" not in result.unmet_needs[0].reason


def test_disabled_experiment_preserves_old_actions(api):
    obs = announced(queue_observation(api), [(1, 0, 2)])
    result = allocator(api, enabled=False).allocate(state(api), [need(due=12)], obs)
    assert result.flows[1] == 5


def test_duplicates_and_publication_order_leave_result_deterministic(api):
    rows = [(3, 0, 5), (3, 0, 4), (1, 1, 2)]
    first = allocator(api).allocate(state(api), [need(due=12)], announced(queue_observation(api), rows))
    second = allocator(api).allocate(state(api), [need(due=12)], announced(queue_observation(api), rows[::-1]))
    np.testing.assert_array_equal(first.flows, second.flows)
    assert first.reasons == second.reasons and first.unmet_needs == second.unmet_needs


def test_no_alternative_reports_unknown_without_spending_stock(api):
    obs = announced(queue_observation(api), [(1, 0, 2)])
    obs["graph_now.u"][6] = 0
    result = allocator(api).allocate(state(api), [need(due=12)], obs)
    assert not np.any(result.flows) and not result.resource_usage
    assert "queue_eta_announced_prohibition_conflict" in result.unmet_needs[0].reason
    assert result.override_qty is None and result.release_mode is None


def test_flag_requires_fifo_and_boolean(api):
    with pytest.raises(ValueError, match="requires queue_eta_enabled"):
        api.Allocator(api.cfg, announced_eta_guard_enabled=True)
    with pytest.raises(ValueError, match="must be a boolean"):
        api.Allocator(api.cfg, queue_eta_enabled=True, announced_eta_guard_enabled="true")


@pytest.mark.parametrize("task", ["tiny", "small", "full"])
def test_opt_in_agent_accepts_real_shapes(tmp_path, task):
    import json
    import shutil

    import gymnasium as gym
    from shockbench_flow_gym import agent_config_from_reset

    from sbf_starter import env_id
    from sbf_starter.agents import load
    from tests.conftest import ROOT

    folder = tmp_path / "guard"
    shutil.copytree(ROOT / "agents" / "team_agent", folder, ignore=shutil.ignore_patterns("__pycache__"))
    (folder / "params.json").write_text(
        json.dumps(
            {
                "allocation_enabled": True,
                "queue_eta_enabled": True,
                "announced_eta_guard_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    env = gym.make(env_id(task), entropy=67890)
    try:
        obs, info = env.reset(seed=0, options={"episode": 0})
        agent = load(folder)(agent_config_from_reset(env, obs, info))
        action = agent.act(obs)
        assert action["flows"].shape == env.action_space["flows"].shape
        assert np.all(np.isfinite(action["flows"])) and np.all(action["flows"] >= 0)
        env.step(action)
    finally:
        env.close()
