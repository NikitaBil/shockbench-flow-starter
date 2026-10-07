"""Route-specific production coverage, shared Fab capacity and hidden ETA."""

import copy
import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests import test_team_analytics as analytics
from tests.conftest import ROOT
from tests.test_nominal_bom import bom_case


analytics_api = analytics.api
resets = analytics.resets


def two_fabs(monkeypatch, *, second_stock=0.0):
    monkeypatch.syspath_prepend(str(ROOT / "agents" / "team_agent"))
    for name in ("needs", "contracts", "observations"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module("needs")
    config, observation, state = bom_case(demand=(8.0, 0.0))
    config["static"]["instance"]["nodes"][2]["fab"] = copy.deepcopy(config["static"]["instance"]["nodes"][1]["fab"])
    state.available_stock[(2, 0)] = SimpleNamespace(value=second_stock)
    observation["graph_now.fab.cap_eff"] = np.asarray([3.0, 10.0])
    planner = module.NeedPlanner(config, production_horizon=1, safety_stock=False)
    return module, planner, observation, state


def routes(observation, entries):
    observation["graph_now.tau"] = np.asarray([entry[2] for entry in entries], dtype=float)
    observation["graph_now.tau.observed"] = np.ones(len(entries), dtype=int)
    slots_to = {}
    result = []
    for slot, (source, destination, _eta) in enumerate(entries):
        slots_to.setdefault((destination, 0), []).append(slot)
        result.append(SimpleNamespace(source_node=source, edges=(slot,)))
    return SimpleNamespace(slots_to=slots_to, routes=tuple(result))


def fab_inputs(requirements):
    return {
        node: [(item[0], item[1]) for item in rows if item[3] == "production"]
        for (node, commodity), rows in requirements.items()
        if node in (1, 2) and commodity == 4
    }


def run_requirements(module, planner, observation, state, network):
    issues = []
    result = planner._requirements(state, module.ObservationReader(observation), issues, network)
    return fab_inputs(result), issues


def test_spare_fab_capacity_covers_remainder(monkeypatch):
    module, planner, observation, state = two_fabs(monkeypatch)
    network = routes(observation, [(1, 0, 0), (2, 0, 0)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(1, 3.0)], 2: [(1, 5.0)]}
    assert not issues


def test_unreachable_fab_stock_cannot_cancel_reachable_production(monkeypatch):
    module, planner, observation, state = two_fabs(monkeypatch, second_stock=100.0)
    network = routes(observation, [(1, 0, 0)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(1, 3.0)]}
    assert any("unplanned_input:0:1:5" in issue for issue in issues)
    assert state.available_stock[(2, 0)].value == 100.0


@pytest.mark.parametrize("missing", ["capacity", "output_stock", "transit"])
def test_unknown_fastest_fab_does_not_block_known_alternative(monkeypatch, missing):
    module, planner, observation, state = two_fabs(monkeypatch)
    network = routes(observation, [(1, 0, 0), (2, 0, 0)])
    if missing == "capacity":
        observation["graph_now.fab.cap_eff.observed"][0] = 0
    elif missing == "output_stock":
        state.available_stock[(1, 0)].value = None
    else:
        observation["graph_now.tau.observed"][0] = 0
    inputs, _issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {2: [(1, 8.0)]}


def dated_demand(planner, observation, state, *, due=8):
    planner.production_horizon = due
    state.horizon = due
    observation["demand_forecast.qty"] = np.zeros((2, due), dtype=float)
    observation["demand_forecast.qty"][0, due - 1] = 8.0
    observation["demand_forecast.qty.observed"] = np.ones((2, due), dtype=int)
    planner.profiles[1]["fab"]["tau"] = 2
    planner.profiles[2]["fab"]["tau"] = 1


def test_alternative_fab_uses_its_own_transit_and_lead_time(monkeypatch):
    module, planner, observation, state = two_fabs(monkeypatch)
    dated_demand(planner, observation, state)
    network = routes(observation, [(1, 0, 1), (2, 0, 3)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(5, 3.0)], 2: [(4, 5.0)]}
    assert not issues


def arrival(node, week, quantity=8.0, *, source="observed"):
    return SimpleNamespace(
        destination_node=node,
        commodity_id=0,
        arrival_week=week,
        quantity=SimpleNamespace(value=quantity, source=source),
        source=source,
    )


@pytest.mark.parametrize(
    "arrival_week,source,include_estimated,expected",
    [
        (5, "observed", False, 0.0),
        (6, "observed", False, 8.0),
        (5, "estimated", False, 8.0),
        (5, "estimated", True, 0.0),
    ],
)
def test_fab_arrivals_cover_only_route_specific_ship_deadline(
    monkeypatch, arrival_week, source, include_estimated, expected
):
    module, planner, observation, state = two_fabs(monkeypatch)
    dated_demand(planner, observation, state)
    observation["graph_now.fab.cap_eff"][0] = 10.0
    planner.include_estimated_arrivals = include_estimated
    state.arrivals = (arrival(1, arrival_week, source=source),)
    network = routes(observation, [(1, 0, 3)])
    inputs, _issues = run_requirements(module, planner, observation, state, network)
    assert sum(quantity for rows in inputs.values() for _week, quantity in rows) == expected
    assert state.arrivals[0].quantity.value == 8.0


def add_second_osat(planner, observation, state):
    planner.config["static"]["nodes"]["id"].append("osat_2")
    profile = {"id": "osat_2", "osat": {"packages": {"raw_a": "pkg_a"}}}
    planner.config["static"]["instance"]["nodes"].append(profile)
    planner.config["layout"]["osats"].append(4)
    planner.profiles[4] = profile
    for commodity in (0, 2):
        state.available_stock[(4, commodity)] = SimpleNamespace(value=0.0)
    observation["graph_now.osat.thr_eff"] = np.asarray([4.0, 100.0])
    observation["graph_now.osat.thr_eff.observed"] = np.ones(2, dtype=int)


@pytest.mark.parametrize("supply_kind", ["stock", "arrival"])
def test_shared_fab_output_is_consumed_once_across_osats(monkeypatch, supply_kind):
    module, planner, observation, state = two_fabs(monkeypatch)
    add_second_osat(planner, observation, state)
    observation["graph_now.fab.cap_eff"][0] = 10.0
    if supply_kind == "stock":
        state.available_stock[(1, 0)].value = 4.0
    else:
        state.arrivals = (arrival(1, 1, 4.0),)
    network = routes(observation, [(1, 0, 0), (1, 4, 0)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(1, 4.0)]}
    assert not issues
    assert state.available_stock[(1, 0)].value == (4.0 if supply_kind == "stock" else 0.0)


def test_shared_fab_capacity_is_not_reused_by_another_osat(monkeypatch):
    module, planner, observation, state = two_fabs(monkeypatch)
    add_second_osat(planner, observation, state)
    observation["graph_now.fab.cap_eff"][0] = 5.0
    network = routes(observation, [(1, 0, 0), (1, 4, 0)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(1, 4.0), (1, 1.0)]}
    assert any("unplanned_input:4:1:3" in issue for issue in issues)


def test_stock_coverage_preserves_receiving_osat(monkeypatch):
    module, planner, observation, state = two_fabs(monkeypatch, second_stock=4.0)
    add_second_osat(planner, observation, state)
    observation["graph_now.fab.cap_eff"][0] = 10.0
    network = routes(observation, [(2, 0, 0), (1, 4, 0)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(1, 4.0)]}
    assert not issues


def test_fab_capacity_is_independent_between_production_weeks(monkeypatch):
    module, planner, observation, state = two_fabs(monkeypatch)
    dated_demand(planner, observation, state, due=3)
    observation["demand_forecast.qty"][0] = [0.0, 3.0, 3.0]
    planner.profiles[1]["fab"]["tau"] = 1
    network = routes(observation, [(1, 0, 0)])
    inputs, issues = run_requirements(module, planner, observation, state, network)
    assert inputs == {1: [(1, 3.0), (2, 3.0)]}
    assert not issues


def test_hidden_pipeline_arrival_week_remains_unknown(analytics_api):
    a = analytics.empty(analytics_api("small"))
    route = next(route for route in a.network.routes if route.lane_id is None)
    analytics.pipeline(a, route, due=1)
    a.obs["pipeline.arrival_week.observed"][0] = 0
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    assert state.arrivals[0].arrival_week is None
    assert state.arrivals[0].source == "unknown"


@pytest.mark.parametrize("hidden", ["edge", "lane"])
def test_hidden_pipeline_route_has_unknown_remaining_time(analytics_api, hidden):
    a = analytics.empty(analytics_api("small"))
    route = next(route for route in a.network.routes if route.lane_id is not None)
    analytics.pipeline(a, route, due=1)
    a.obs[f"pipeline.{hidden}.observed"][0] = 0
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    assert state.pipeline[0].remaining_route_weeks is None
    assert not state.arrivals
