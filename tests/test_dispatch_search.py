"""Active allocator search: forecasts only for candidates that can win."""

import importlib

import numpy as np

from tests.test_allocation import api as api
from tests.test_allocation import need, queue_observation, state


def test_cost_dominated_sea_alternative_saves_a_forecast(api):
    api.cfg["static"]["edges"]["head"][2] = 4
    api.cfg["static"]["edges"]["c0"][3] = 30
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    events = []
    obj.trace_callback = events.append
    result = obj.allocate(state(api), [need(quantity=1, due=12)], obs)
    assert result.flows[0] == 1 and result.flows[1] == 0
    assert any(r.code == "queue_eta_forecast_usage" and "Ran 1/16" in r.message for r in result.reasons)
    assert any(e["slot_id"] == 1 and e["reason"] == "dominated_delivery_candidate" for e in events)


def test_cheaper_direct_route_avoids_unnecessary_joint_forecast(api):
    obs = queue_observation(api)
    obs["graph_now.c"][6] = 1  # Cheap direct route is evaluated first.
    queue = api.c.QueueLot("old", 1, 0, 1, "known", 1, 0, api.c.Quantity(20, "observed"))
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    result = obj.allocate(state(api, queues=(queue,)), [need(quantity=1, due=12)], obs)
    assert result.flows[4] == 1
    assert any(r.code == "queue_eta_forecast_usage" and "Ran 0/16" in r.message for r in result.reasons)


def test_new_need_rebuilds_bound_after_shared_stock_spent(api):
    api.cfg["static"]["edges"]["head"][2] = 4
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    result = obj.allocate(state(api, stock=3), [need("a", quantity=2), need("b", quantity=2)], obs)
    assert result.flows.sum() == 3
    assert result.unmet_needs[0].remaining_quantity == 1
    assert sum(u.used for u in result.resource_usage if u.kind == "stock") == 3


def test_search_does_not_raise_forecast_budget(api):
    eta = importlib.import_module("delivery_eta").CandidateETA
    assert eta.MAX_FORECASTS == 16 and eta.MAX_WEEKS == 24


def test_optimistic_key_never_reports_unknown_as_on_time(api):
    obs = queue_observation(api)
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    snapshot = obj._snapshot(obs)
    option = obj.delivery.options(snapshot, 4, 0)[0]
    assert option.estimated_completion_week is None
    obj._optimistic_key(option, need(), 5, snapshot)
    assert option.estimated_completion_week is None
    assert np.all(obs["stock.qty"] == [20, 7, 5])
