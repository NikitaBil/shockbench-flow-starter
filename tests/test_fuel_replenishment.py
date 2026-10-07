"""Terminal replenishment regressions kept separate from production planning tests."""

import importlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests import test_team_analytics as analytics


api = analytics.api
resets = analytics.resets
empty = analytics.empty


def fuel_replenishment_case(api, task="small"):
    a = empty(api(task))
    grids = set(a.config["layout"]["grids"])
    route = next(
        route
        for route in a.network.routes
        if route.destination_node in grids
        and a.network.commodity_names[route.commodity_id] == "lng"
        and a.network.slots_to.get((route.source_node, route.commodity_id))
    )
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    need = a.needs.DeliveryNeed("fuel-test", route.destination_node, route.commodity_id, 10, 7, 3, "grid_fuel", None)
    pair = route.source_node, route.commodity_id
    network = SimpleNamespace(
        routes=a.network.routes,
        slots_to={
            (route.destination_node, route.commodity_id): (route.slot_id,),
            pair: a.network.slots_to[pair],
        },
    )
    return a, state, route, need, network


def test_replenishment_creates_real_terminal_request_with_pre_dispatch_lead(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    planner = a.needs.NeedPlanner(a.config)
    result = planner._fuel_replenishment(state, (need,), network, [])
    assert len(result) == 1
    assert result[0].destination_node == route.source_node
    assert result[0].quantity == 10
    assert result[0].due_week == max(state.week, need.due_week - route.nominal_transit_weeks - 1)
    assert result[0].reason == "fuel_replenishment"
    assert result[0].confidence is None


@pytest.mark.parametrize("arrival_week", [None, 2, 10])
def test_import_inventory_offsets_orders_once_not_consumer_deadline_coverage(api, arrival_week):
    a, state, route, need, network = fuel_replenishment_case(api)
    pair = route.source_node, route.commodity_id
    stocks = dict(state.available_stock)
    stocks[pair] = a.contracts.Quantity(3, "observed")
    cargo = a.contracts.PipelineLot(
        "cargo",
        route.edge_id,
        route.commodity_id,
        None,
        "off_lane",
        a.contracts.Quantity(4, "observed"),
        2,
        route.source_node,
    )
    arrival = a.contracts.ExpectedArrival(
        "arrival",
        "cargo",
        "pipeline",
        *pair,
        cargo.quantity,
        arrival_week,
        "unknown" if arrival_week is None else "observed",
    )
    state = replace(state, available_stock=stocks, pipeline=(cargo,), arrivals=(arrival,))
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(state, (need,), network, [])
    assert sum(item.quantity for item in result) == 3
    assert state.available_stock[pair].value == 3
    assert need.quantity == 10 and need.due_week == 7
    assert any("not timely coverage" in assumption for assumption in result[0].assumptions)


@pytest.mark.parametrize("case", ["unknown_quantity", "after_horizon", "other_terminal", "wip"])
def test_import_reservations_do_not_fabricate_or_misattribute_inventory(api, case):
    a, state, route, need, network = fuel_replenishment_case(api)
    pair = route.source_node, route.commodity_id
    arrival = a.contracts.ExpectedArrival(
        "arrival", "cargo", "pipeline", *pair, a.contracts.Quantity(10, "observed"), None, "unknown"
    )
    if case == "unknown_quantity":
        arrival = replace(arrival, quantity=a.contracts.Quantity(None, "unknown"))
    elif case == "after_horizon":
        arrival = replace(arrival, arrival_week=state.horizon + 1)
    elif case == "other_terminal":
        arrival = replace(arrival, destination_node=route.destination_node)
    else:
        arrival = replace(arrival, source_kind="wip")
    state = replace(state, arrivals=(arrival,))
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(state, (need,), network, [])
    assert sum(item.quantity for item in result) == 10


def test_shared_terminal_position_is_not_reused_for_multiple_consumers(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    other_grid = next(node for node in a.config["layout"]["grids"] if node != route.destination_node)
    second = replace(route, slot_id=len(network.routes), destination_node=other_grid)
    network.routes += (second,)
    network.slots_to[other_grid, route.commodity_id] = (second.slot_id,)
    stocks = dict(state.available_stock)
    stocks[route.source_node, route.commodity_id] = a.contracts.Quantity(5, "observed")
    state = replace(state, available_stock=stocks)
    other_need = replace(need, need_id="other-grid", destination_node=other_grid)
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(state, (need, other_need), network, [])
    assert sum(item.quantity for item in result) == 15
    assert len({item.need_id for item in result}) == len(result)


def test_alternative_feeders_do_not_duplicate_the_same_deficit(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    other = next(
        r
        for r in a.network.routes
        if r.commodity_id == route.commodity_id
        and r.destination_node in a.config["layout"]["grids"]
        and r.source_node != route.source_node
        and a.network.slots_to.get((r.source_node, r.commodity_id))
    )
    alternative = replace(other, slot_id=len(network.routes), destination_node=route.destination_node)
    network.routes += (alternative,)
    network.slots_to[route.destination_node, route.commodity_id] += (alternative.slot_id,)
    network.slots_to[other.source_node, route.commodity_id] = a.network.slots_to[other.source_node, route.commodity_id]
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(state, (need,), network, [])
    assert sum(item.quantity for item in result) == 10


def test_unknown_terminal_stock_is_not_spent_or_treated_as_observed_zero(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    stocks = dict(state.available_stock)
    stocks[route.source_node, route.commodity_id] = a.contracts.Quantity(None, "unknown")
    issues = []
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(
        replace(state, available_stock=stocks), (need,), network, issues
    )
    assert not result
    assert any("unknown_inventory" in issue for issue in issues)


def test_fully_ordered_import_is_not_ordered_again_next_week(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    arrival = a.contracts.ExpectedArrival(
        "arrival",
        "cargo",
        "queue",
        route.source_node,
        route.commodity_id,
        a.contracts.Quantity(10, "observed"),
        None,
        "unknown",
    )
    planner = a.needs.NeedPlanner(a.config)
    for week in (state.week, state.week + 1):
        assert not planner._fuel_replenishment(replace(state, week=week, arrivals=(arrival,)), (need,), network, [])


def test_estimated_calendar_does_not_duplicate_observed_pipeline_inventory(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    pair = route.source_node, route.commodity_id
    cargo = a.contracts.PipelineLot(
        "cargo",
        route.edge_id,
        route.commodity_id,
        None,
        "off_lane",
        a.contracts.Quantity(4, "observed"),
        2,
        route.source_node,
    )
    pieces = tuple(
        a.contracts.ExpectedArrival(
            f"piece-{i}", "cargo", "pipeline", *pair, a.contracts.Quantity(2, "estimated"), 5 + i, "estimated"
        )
        for i in range(2)
    )
    state = replace(state, pipeline=(cargo,), arrivals=pieces)
    for include_estimated in (False, True):
        result = a.needs.NeedPlanner(a.config, include_estimated_arrivals=include_estimated)._fuel_replenishment(
            state, (need,), network, []
        )
        assert sum(item.quantity for item in result) == 6


def test_queued_import_inventory_is_reserved_at_its_final_terminal(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    incoming = next(
        a.network.routes[slot]
        for slot in a.network.slots_to[route.source_node, route.commodity_id]
        if a.network.routes[slot].lane_id is not None
    )
    network.transit_progress = a.network.transit_progress
    edge = incoming.edges[-1]
    queue = a.contracts.QueueLot(
        "queued",
        a.network.edge_tail[edge],
        route.commodity_id,
        incoming.lane_id,
        "known",
        edge,
        0,
        a.contracts.Quantity(4, "observed"),
    )
    state = replace(state, queues=(queue,))
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(state, (need,), network, [])
    assert sum(item.quantity for item in result) == 6


def test_overdue_consumer_request_stays_overdue_but_import_due_is_current_week(api):
    a, state, route, need, network = fuel_replenishment_case(api)
    state = replace(state, week=5)
    need = replace(need, due_week=2)
    result = a.needs.NeedPlanner(a.config)._fuel_replenishment(state, (need,), network, [])
    assert result[0].due_week == 5
    assert need.due_week == 2


@pytest.mark.parametrize("task", ["small", "full"])
def test_real_planner_and_allocator_dispatch_imports_for_grid_fuel(api, task):
    a = empty(api(task))
    a.obs["stock.qty"][:] = 0
    stock_pairs = list(map(tuple, a.config["layout"]["stock_slots"]))
    grids = set(a.config["layout"]["grids"])
    importer = next(
        r
        for r in a.network.routes
        if r.lane_id is not None
        and a.network.commodity_names[r.commodity_id] == "lng"
        and any(
            out.destination_node in grids
            and out.source_node == r.destination_node
            and out.commodity_id == r.commodity_id
            for out in a.network.routes
        )
    )
    a.obs["stock.qty"][stock_pairs.index((importer.source_node, importer.commodity_id))] = 100000
    state = a.state.StateBuilder(a.config).build(a.obs, a.network)
    enabled = a.needs.NeedPlanner(a.config, production_enabled=False, safety_stock=False)
    needs = enabled.plan(state, a.obs, a.network)
    imports = [
        need
        for need in needs
        if need.reason == "fuel_replenishment"
        and need.destination_node == importer.destination_node
        and need.commodity_id == importer.commodity_id
    ]
    assert imports
    disabled = a.needs.NeedPlanner(
        a.config, production_enabled=False, safety_stock=False, fuel_replenishment_enabled=False
    )
    assert not any(need.reason == "fuel_replenishment" for need in disabled.plan(state, a.obs, a.network))
    # A fresh, fully visible open network isolates the missing-demand regression.
    a.obs["action_mask"][:] = 1
    a.obs["action_mask.observed"][:] = 1
    a.obs["graph_now.open"][:] = 1
    a.obs["graph_now.open.observed"][:] = 1
    a.obs["graph_now.prohibited"][:] = 0
    a.obs["graph_now.prohibited.observed"][:] = 1
    a.obs["graph_now.tau.observed"][:] = 1
    a.obs["graph_now.u"][:] = 100000
    a.obs["graph_now.u.observed"][:] = 1
    for pool in ("tb", "ct"):
        a.obs[f"graph_now.kappa.{pool}"][:] = 100000
        a.obs[f"graph_now.kappa.{pool}.observed"][:] = 1
    allocator = importlib.import_module("allocation").Allocator(a.config, a.network, queue_eta_enabled=True)
    result = allocator.allocate(state, imports, a.obs, a.network)
    assert any(
        result.flows[r.slot_id] > 0
        for r in a.network.routes
        if r.destination_node == importer.destination_node and r.lane_id is not None
    )

