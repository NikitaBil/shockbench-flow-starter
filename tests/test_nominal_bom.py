"""Hand-calculated nominal BOM regressions independent of benchmark episodes."""

import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from tests.conftest import ROOT


@pytest.fixture
def needs_module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "agents" / "team_agent"))
    for name in ("needs", "contracts", "observations"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("needs")


def bom_case(w_scr=2.0, tau=8.0, demand=(2.0, 3.0)):
    """Two OSAT input/output mappings and two independent one-to-one Fabs."""
    commodities = ["raw_a", "raw_b", "pkg_a", "pkg_b", "ore_a", "ore_b"]
    nodes = ["osat", "fab_a", "fab_b", "sink"]
    instance_nodes = [
        {"id": "osat", "osat": {"packages": {"raw_a": "pkg_a", "raw_b": "pkg_b"}}},
        {"id": "fab_a", "fab": {"product": "raw_a", "input": "ore_a", "w_scr": w_scr, "tau": tau}},
        {"id": "fab_b", "fab": {"product": "raw_b", "input": "ore_b", "w_scr": w_scr, "tau": tau}},
        {"id": "sink"},
    ]
    config = {
        "T": 1,
        "static": {
            "commodities": {"id": commodities},
            "nodes": {"id": nodes},
            "instance": {"nodes": instance_nodes},
            "sinks": {"node": [3, 3], "k": [2, 3], "pi": [1.0, 1.0]},
        },
        "layout": {"demands": [[3, 2], [3, 3]], "osats": [0], "fabs": [1, 2], "grids": []},
    }
    observation = {
        "demand_forecast.qty": np.asarray(demand, dtype=float).reshape(2, 1),
        "demand_forecast.qty.observed": np.ones((2, 1), dtype=int),
        "graph_now.osat.thr_eff": np.asarray([100.0]),
        "graph_now.osat.thr_eff.observed": np.ones(1, dtype=int),
        "graph_now.fab.cap_eff": np.asarray([100.0, 100.0]),
        "graph_now.fab.cap_eff.observed": np.ones(2, dtype=int),
    }
    available_stock = {
        (0, 2): SimpleNamespace(value=0.0),
        (0, 3): SimpleNamespace(value=0.0),
        (0, 0): SimpleNamespace(value=0.0),
        (0, 1): SimpleNamespace(value=0.0),
        (1, 0): SimpleNamespace(value=0.0),
        (2, 1): SimpleNamespace(value=0.0),
    }
    state = SimpleNamespace(week=1, horizon=1, available_stock=available_stock, arrivals=())
    return config, observation, state


def production_input_requirements(module, config, observation, state, *, policy=None):
    planner = module.NeedPlanner(
        config,
        production_horizon=1,
        safety_stock=False,
        safety_buffer_policy=policy,
    )
    requirements = planner._requirements(state, module.ObservationReader(observation), [])
    return {
        pair: sum(item[1] for item in requests if item[3] == "production")
        for pair, requests in requirements.items()
        if any(item[3] == "production" for item in requests)
    }


def test_hand_calculated_multi_input_nominal_bom(needs_module):
    config, observation, state = bom_case()

    assert production_input_requirements(needs_module, config, observation, state) == {
        (0, 0): 2.0,  # OSAT raw_a for 2 pkg_a
        (0, 1): 3.0,  # OSAT raw_b for 3 pkg_b
        (1, 4): 2.0,  # Fab ore_a for 2 raw_a
        (2, 5): 3.0,  # Fab ore_b for 3 raw_b
    }


def test_zero_demand_creates_no_nominal_bom_inputs(needs_module):
    config, observation, state = bom_case(demand=(0.0, 0.0))

    assert production_input_requirements(needs_module, config, observation, state) == {}


@pytest.mark.parametrize("scrap_window,tau", [(0.0, 1.0), (2.0, 8.0), (1_000_000.0, 0.25)])
def test_nominal_bom_is_invariant_to_scrap_window_and_duration(needs_module, scrap_window, tau):
    baseline = bom_case(w_scr=2.0, tau=8.0)
    changed = bom_case(w_scr=scrap_window, tau=tau)

    baseline_inputs = production_input_requirements(needs_module, *baseline)
    changed_inputs = production_input_requirements(needs_module, *changed)

    assert baseline_inputs == changed_inputs
    assert baseline_inputs[(1, 4)] == 2.0
    assert baseline_inputs[(2, 5)] == 3.0


def test_explicit_safety_buffer_is_added_above_nominal(needs_module):
    config, observation, state = bom_case()
    nominal = production_input_requirements(needs_module, config, observation, state)
    policy = needs_module.SafetyBufferPolicy(input_buffer_fraction=0.25)
    buffered = production_input_requirements(needs_module, config, observation, state, policy=policy)

    assert nominal == {(0, 0): 2.0, (0, 1): 3.0, (1, 4): 2.0, (2, 5): 3.0}
    # The explicit policy buffers both OSAT and Fab conversion inputs. Fab
    # requirements include the already-buffered raw requirements upstream.
    assert buffered == {(0, 0): 2.5, (0, 1): 3.75, (1, 4): 3.125, (2, 5): 4.6875}
    assert nominal == production_input_requirements(needs_module, config, observation, state)


def test_upstream_requirements_are_time_phased_by_production_lead(needs_module):
    config, observation, state = bom_case(demand=(0.0, 0.0))
    config["T"] = 4
    state.horizon = 4
    config["static"]["instance"]["nodes"][0]["osat"]["tau"] = 1
    config["static"]["instance"]["nodes"][1]["fab"]["tau"] = 2
    config["static"]["instance"]["nodes"][2]["fab"]["tau"] = 2
    observation["demand_forecast.qty"] = np.zeros((2, 4), dtype=float)
    observation["demand_forecast.qty"][0, 3] = 2.0
    observation["demand_forecast.qty.observed"] = np.ones((2, 4), dtype=int)
    planner = needs_module.NeedPlanner(config, production_horizon=4, safety_stock=False)
    requirements = planner._requirements(state, needs_module.ObservationReader(observation), [])

    osat_need = requirements[(0, 0)][0]
    fab_need = requirements[(1, 4)][0]
    assert (osat_need[0], osat_need[1]) == (3, 2.0)  # Week 4 package less 1 OSAT week.
    assert (fab_need[0], fab_need[1]) == (1, 2.0)  # OSAT start week 3 less 2 Fab weeks.


def test_fab_schedule_backs_out_live_route_eta(needs_module):
    config, observation, state = bom_case(demand=(0.0, 0.0))
    config["T"] = 9
    state.horizon = 9
    config["static"]["instance"]["nodes"][0]["osat"]["tau"] = 1
    config["static"]["instance"]["nodes"][1]["fab"]["tau"] = 2
    observation["demand_forecast.qty"] = np.zeros((2, 9), dtype=float)
    observation["demand_forecast.qty"][0, 8] = 2.0
    observation["demand_forecast.qty.observed"] = np.ones((2, 9), dtype=int)
    observation["graph_now.tau"] = np.asarray([3.0])
    observation["graph_now.tau.observed"] = np.ones(1, dtype=int)
    route = SimpleNamespace(source_node=1, edges=(0,))
    network = SimpleNamespace(routes=(route,), slots_to={(0, 0): (0,)})
    planner = needs_module.NeedPlanner(config, production_horizon=9, safety_stock=False)
    requirements = planner._requirements(
        state, needs_module.ObservationReader(observation), [], network
    )

    demand_week = 9
    osat_lead_weeks = 1
    fab_to_osat_transit_weeks = 3
    fab_lead_weeks = 2
    osat_input_receipt_week = demand_week - osat_lead_weeks
    fab_output_receipt_week = osat_input_receipt_week - fab_to_osat_transit_weeks
    fab_input_order_week = fab_output_receipt_week - fab_lead_weeks

    assert requirements[(0, 0)][0][0] == osat_input_receipt_week == 8
    assert requirements[(1, 4)][0][0] == fab_input_order_week == 3
    # A Fab input need is due by production start; production plus outbound
    # transit must meet the downstream OSAT input receipt SLA.
    assert fab_input_order_week + fab_lead_weeks + fab_to_osat_transit_weeks <= osat_input_receipt_week
