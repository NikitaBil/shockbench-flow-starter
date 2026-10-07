"""Missing shortage valuation must not silently mean zero lateness damage."""

from dataclasses import replace

import pytest

from tests.test_allocation import api as api
from tests.test_allocation import need, queue_observation, state


@pytest.mark.parametrize("penalty,slot", [(None, 4), (0, 1), (100, 4)])
def test_unknown_cost_and_explicit_zero_cost_are_distinct(api, penalty, slot):
    request = need(due=1)
    request.shortage_cost_per_unit_usd = penalty
    result = api.allocator().allocate(state(api), [request], queue_observation(api))
    assert result.flows[slot] == 5
    assert any(r.code == "eta_late" for r in result.reasons)
    assert not any(r.code == "eta_on_time_estimate" for r in result.reasons)


def test_unpriced_late_sea_candidates_forecast_fastest_first(api):
    api.cfg["static"]["edges"]["head"][2] = 4
    api.cfg["static"]["edges"]["c0"][2] = 50  # Faster lane is more expensive.
    obs = queue_observation(api)
    obs["graph_now.u"][6] = 0
    request = need(quantity=1, due=1)
    request.shortage_cost_per_unit_usd = None
    obj = api.Allocator(api.cfg, queue_eta_enabled=True)
    events = []
    obj.trace_callback = events.append
    result = obj.allocate(state(api), [request], obs)
    assert next(e for e in events if e["stage"] == "eta")["slot_id"] == 0
    assert result.flows[0] == 1 and result.flows[1] == 0


def test_valued_need_preserves_transport_delay_cost_tradeoff(api):
    obj = api.allocator()
    snapshot = obj._snapshot(queue_observation(api))
    original = obj.delivery.options(snapshot, 4, 0)[0]
    cheap = replace(original, estimated_completion_week=10, quantity=5, transport_cost_per_unit=1, slot_id=1)
    fast = replace(original, estimated_completion_week=8, quantity=5, transport_cost_per_unit=20, slot_id=4)
    request = need(due=5)
    request.shortage_cost_per_unit_usd = 1
    assert obj._candidate_key(cheap, request, 5)[0] < obj._candidate_key(fast, request, 5)[0]
    request.shortage_cost_per_unit_usd = 100
    assert obj._candidate_key(fast, request, 5)[0] < obj._candidate_key(cheap, request, 5)[0]


def test_unknown_timing_is_still_behind_known_late_route(api):
    obj = api.allocator()
    original = obj.delivery.options(obj._snapshot(queue_observation(api)), 4, 0)[0]
    unknown = replace(original, estimated_completion_week=None, transport_cost_per_unit=0)
    known = replace(original, estimated_completion_week=10, quantity=5, transport_cost_per_unit=100)
    request = need(due=1)
    request.shortage_cost_per_unit_usd = None
    assert obj._candidate_key(known, request, 5)[0] < obj._candidate_key(unknown, request, 5)[0]
    assert obj._candidate_key(unknown, request, 5)[1] is None


def test_unpriced_unknown_routes_use_lower_bound_without_certifying_eta(api):
    obj = api.allocator()
    original = obj.delivery.options(obj._snapshot(queue_observation(api)), 4, 0)[0]
    faster = replace(
        original, estimated_completion_week=None, quantity=1, no_wait_arrival_week=4, transport_cost_per_unit=50
    )
    cheaper = replace(
        original, estimated_completion_week=None, quantity=1, no_wait_arrival_week=8, transport_cost_per_unit=1
    )
    request = need(due=1)
    request.shortage_cost_per_unit_usd = None
    assert obj._candidate_key(faster, request, 1)[0] < obj._candidate_key(cheaper, request, 1)[0]
    assert obj._candidate_key(faster, request, 1)[1] is None


def test_optimistic_rank_remains_a_lower_bound_for_unpriced_late_need(api):
    obj = api.allocator()
    snapshot = obj._snapshot(queue_observation(api))
    option = obj.delivery.options(snapshot, 4, 0)[0]
    actual = replace(option, estimated_completion_week=10, quantity=5, queue_holding_cost_per_unit=50)
    request = need(due=1)
    request.shortage_cost_per_unit_usd = None
    assert obj._optimistic_key(option, request, 5, snapshot) <= obj._candidate_key(actual, request, 5)[0]
