"""Bounded compute may truncate ETA, but must not fabricate a timely arrival."""

import importlib

import numpy as np
import pytest

from tests.test_allocation import api as api
from tests.test_allocation import need, queue_observation, state


def no_forecast(api, monkeypatch):
    monkeypatch.setattr(importlib.import_module("delivery_eta").CandidateETA, "MAX_FORECASTS", 0)
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    return obs


def test_budget_cutoff_dispatches_without_a_timing_claim(api, monkeypatch):
    obs = no_forecast(api, monkeypatch)
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    events = []
    obj.trace_callback = events.append
    result = obj.allocate(state(api), [need()], obs)
    assert result.flows[1] == 5 and not result.unmet_needs
    assert any(r.code == "dispatch_without_certified_eta" for r in result.reasons)
    assert not any(r.code in ("eta_on_time_estimate", "eta_late", "queue_eta_estimate") for r in result.reasons)
    event = next(e for e in events if e["stage"] == "assignment")
    assert event["eta"] is None and event["late_weeks"] is None
    assert event["reason"] == "delivery_eta_unknown"
    assert result.override_qty is None and result.release_mode is None


def test_known_direct_completion_beats_unknown_sea(api, monkeypatch):
    obs = no_forecast(api, monkeypatch)
    obs["graph_now.u"][6] = 9
    result = api.Allocator(api.cfg, queue_eta_enabled=True).allocate(state(api), [need()], obs)
    assert result.flows[4] == 5 and result.flows[1] == 0
    assert any(r.code == "eta_on_time_estimate" for r in result.reasons)


def test_unknown_assignments_share_stock_and_entry_capacity(api, monkeypatch):
    obs = no_forecast(api, monkeypatch)
    requests = [need("a", quantity=5), need("b", destination=3, quantity=5)]
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    first = obj.allocate(state(api, stock=6), requests, obs)
    second = obj.allocate(state(api, stock=6), requests[::-1], obs)
    np.testing.assert_array_equal(first.flows, second.flows)
    assert first.flows[1] == 5 and first.flows[0] == 1
    assert first.unmet_needs[0].need_id == "b" and first.unmet_needs[0].remaining_quantity == 4
    assert sum(u.used for u in first.resource_usage if u.kind == "stock") == 6
    assert first.reasons == second.reasons


@pytest.mark.parametrize("field,index", [("graph_now.tau", 3), ("graph_now.u", 3), ("graph_now.kappa.tb", 0)])
def test_budget_fallback_requires_observed_physical_route(api, monkeypatch, field, index):
    obs = no_forecast(api, monkeypatch)
    obs[field + ".observed"][index] = 0
    result = api.Allocator(api.cfg, queue_eta_enabled=True).allocate(state(api), [need()], obs)
    assert not np.any(result.flows)
    assert "delivery_eta_unknown" in result.unmet_needs[0].reason


def test_budget_fallback_does_not_erase_hidden_joint_inputs(api, monkeypatch):
    obs = no_forecast(api, monkeypatch)
    obs["stock.qty.observed"][1] = 0
    result = api.Allocator(api.cfg, queue_eta_enabled=True).allocate(state(api), [need()], obs)
    assert not np.any(result.flows)


@pytest.mark.parametrize("blocked", ["stock", "permission", "closed", "capacity", "horizon"])
def test_budget_cutoff_does_not_bypass_physical_constraints(api, monkeypatch, blocked):
    obs = no_forecast(api, monkeypatch)
    snapshot = state(api)
    if blocked == "stock":
        snapshot = state(api, stock=None, source="unknown")
    elif blocked == "permission":
        obs["graph_now.prohibited"][3, 0] = 1
        obs["action_mask"][1] = 0
    elif blocked == "closed":
        obs["graph_now.open"][0] = obs["graph_now.kappa.tb"][0] = 0
    elif blocked == "capacity":
        obs["graph_now.u"][0] = 0
    else:
        obs["week"][0] = 10
        snapshot = state(api, week=10)
    result = api.Allocator(api.cfg, queue_eta_enabled=True).allocate(snapshot, [need()], obs)
    assert not np.any(result.flows) and result.unmet_needs


def test_bounded_window_exhaustion_is_not_a_known_missed_episode(api, monkeypatch):
    monkeypatch.setattr(importlib.import_module("delivery_eta").CandidateETA, "MAX_WEEKS", 2)
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    result = api.Allocator(api.cfg, queue_eta_enabled=True).allocate(state(api), [need()], obs)
    assert result.flows[1] == 5
    assert any(r.code == "delivery_eta_unknown" for r in result.reasons)
    assert not any(r.code == "eta_on_time_estimate" for r in result.reasons)


def test_full_episode_forecast_failure_still_rejects_whole_batch(api):
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    queue = api.c.QueueLot("old", 1, 0, 1, "known", 1, 0, api.c.Quantity(1000, "observed"))
    result = api.Allocator(api.cfg, queue_eta_enabled=True).allocate(state(api, queues=(queue,)), [need()], obs)
    assert not np.any(result.flows)
    assert "queue_eta_completion_unresolved" in result.unmet_needs[0].reason


def test_unforecast_cargo_withdraws_previous_joint_eta_labels(api, monkeypatch):
    monkeypatch.setattr(importlib.import_module("delivery_eta").CandidateETA, "MAX_FORECASTS", 1)
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    events = []
    obj.trace_callback = events.append
    result = obj.allocate(state(api), [need("a", quantity=1), need("b", destination=3, quantity=1)], obs)
    assert result.flows[0] == result.flows[1] == 1
    assert any(r.code == "queue_eta_estimate_superseded" and r.need_id == "a" for r in result.reasons)
    assert not any(r.code in ("eta_on_time_estimate", "eta_late", "queue_eta_estimate") for r in result.reasons)
    assert all(e["eta"] is None for e in events if e["stage"] == "assignment")


def test_unknown_proposal_is_included_in_subsequent_joint_forecasts(api, monkeypatch):
    eta_api = importlib.import_module("delivery_eta")
    obs = queue_observation(api)
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    snapshot = obj._snapshot(obs)
    predictor = eta_api.CandidateETA(obj.queue_forecaster, state(api), obs, obj.network, snapshot)
    sea = obj.delivery.options(snapshot, 4, 0)[0]
    predictor.accept(sea, 2, None)
    captured = []
    original = predictor.forecaster.forecast

    def forecast(*args, **kwargs):
        captured.extend(kwargs["proposed_pipeline"])
        return original(*args, **kwargs)

    monkeypatch.setattr(predictor.forecaster, "forecast", forecast)
    predictor.evaluate(sea, 1)
    assert len(captured) == 2 and captured[0].quantity.value == 2
    assert predictor.completions == {}  # No invented completion for the unforecast shipment.


def test_announced_ban_guard_is_not_bypassed_by_budget_cutoff(api, monkeypatch):
    from tests.test_announced_delivery_eta import announced

    obs = announced(no_forecast(api, monkeypatch), [(3, 0, 9)])
    result = api.Allocator(api.cfg, queue_eta_enabled=True, announced_eta_guard_enabled=True).allocate(
        state(api), [need()], obs
    )
    assert not np.any(result.flows)
