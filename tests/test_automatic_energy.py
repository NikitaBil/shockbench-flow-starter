"""Grid fuel covers the simulator's automatic Fab draw, including restoration."""

from types import SimpleNamespace

import numpy as np
import pytest

from tests import test_nominal_bom as bom


needs_module = bom.needs_module


def energy_case(horizon=3):
    config, obs, state = bom.bom_case(demand=(0.0, 0.0))
    config["T"] = state.horizon = horizon
    config["layout"]["grids"] = [4]
    config["static"]["nodes"]["id"].append("grid")
    config["static"]["commodities"]["id"].append("lng")
    config["static"]["instance"]["nodes"].append(
        {"id": "grid", "grid": {"shares": {"lng": 1.0}, "base_load": 999.0}}
    )
    fab = config["static"]["instance"]["nodes"][1]["fab"]
    fab.update(grid="grid", e=1.0)
    state.available_stock[(1, 4)] = SimpleNamespace(value=12.0)
    state.available_stock[(4, 6)] = SimpleNamespace(value=0.0)
    obs.update({
        "demand_forecast.qty": np.zeros((2, horizon)),
        "demand_forecast.qty.observed": np.ones((2, horizon), dtype=int),
        "graph_now.fab.R": np.asarray([0.5, 1.0]),
        "graph_now.fab.R.observed": np.ones(2, dtype=int),
        "graph_now.fab.cap_eff": np.asarray([5.0, 100.0]),
        "graph_now.grid.G_bar": np.asarray([1000.0]),
        "graph_now.grid.G_bar.observed": np.ones(1, dtype=int),
        "graph_now.grid.y_bar": np.asarray([10.0]),
        "graph_now.grid.y_bar.observed": np.ones(1, dtype=int),
    })
    return config, obs, state


def test_automatic_draw_exists_without_planned_production(needs_module):
    config, obs, state = energy_case()
    planner = needs_module.NeedPlanner(config, production_horizon=3, production_enabled=False, safety_stock=False)
    rows = planner._requirements(state, needs_module.ObservationReader(obs), [])[(4, 6)]
    assert [(row[0], row[1]) for row in rows] == [(1, 20.0), (2, 20.0), (3, 14.0)]
    assert state.available_stock[(1, 4)].value == 12


@pytest.mark.parametrize("restoration,mask,expected", [(1.0, 1, 15), (0.0, 1, 10), (0.5, 0, 10)])
def test_restoration_is_masked_and_never_divides_by_zero(needs_module, restoration, mask, expected):
    config, obs, state = energy_case(1)
    obs["graph_now.fab.R"][0] = restoration
    obs["graph_now.fab.R.observed"][0] = mask
    issues = []
    planner = needs_module.NeedPlanner(config, production_enabled=False, safety_stock=False)
    rows = planner._requirements(state, needs_module.ObservationReader(obs), issues)[(4, 6)]
    assert rows[0][1] == expected
    if mask == 0:
        assert any("unknown_automatic_energy" in issue for issue in issues)


def test_unknown_base_load_is_not_replaced_by_static_load(needs_module):
    config, obs, state = energy_case()
    obs["graph_now.grid.y_bar.observed"][:] = 0
    issues = []
    planner = needs_module.NeedPlanner(config, production_enabled=False)
    assert (4, 6) not in planner._requirements(state, needs_module.ObservationReader(obs), issues)
    assert "grid:4:unknown_base_load" in issues


def test_same_week_observed_input_arrival_can_power_automatic_production(needs_module):
    config, obs, state = energy_case(1)
    state.available_stock[(1, 4)].value = 0
    state.arrivals = (SimpleNamespace(
        destination_node=1, commodity_id=4, arrival_week=1, source="observed",
        quantity=SimpleNamespace(value=5, source="observed"),
    ),)
    planner = needs_module.NeedPlanner(config, production_enabled=False, safety_stock=False)
    rows = planner._requirements(state, needs_module.ObservationReader(obs), [])[(4, 6)]
    assert rows[0][1] == 20


def test_planned_and_automatic_draw_are_merged_per_fab_not_per_grid(needs_module):
    config, obs, state = energy_case(1)
    config["static"]["instance"]["nodes"][2]["fab"].update(grid="grid", e=1.0)
    obs["demand_forecast.qty"][1, 0] = 3
    planner = needs_module.NeedPlanner(config, production_enabled=True, safety_stock=False)
    rows = planner._requirements(state, needs_module.ObservationReader(obs), [])[(4, 6)]
    assert rows[0][1] == 23  # base 10 + first Fab automatic 10 + second Fab planned 3
